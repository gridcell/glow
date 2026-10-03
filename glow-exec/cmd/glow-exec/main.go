// Command glow-exec is the step runtime. It runs inside a toolpack
// container, prepares the tool's inputs, runs the tool and publishes its
// outputs. See docs/glow-exec.md.
//
//	glow-exec run --tool <name@major> [flags] [-- <command...>]
//	glow-exec stage --tool <name@major> [flags]
//	glow-exec collect --tool <name@major> [flags]
//	glow-exec eval [--env <json>|@file] [--template] <expression>
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"

	// Certificate roots for S3 over TLS when the image has none, as in a
	// scratch-based init container. The system roots win when present.
	_ "golang.org/x/crypto/x509roots/fallback"

	"github.com/sparkgeo/glow/glow-exec/internal/cel"
	"github.com/sparkgeo/glow/glow-exec/internal/collect"
	"github.com/sparkgeo/glow/glow-exec/internal/jsonvalue"
	"github.com/sparkgeo/glow/glow-exec/internal/manifest"
	"github.com/sparkgeo/glow/glow-exec/internal/params"
	"github.com/sparkgeo/glow/glow-exec/internal/prefix"
	"github.com/sparkgeo/glow/glow-exec/internal/run"
	"github.com/sparkgeo/glow/glow-exec/internal/stage"
	"github.com/sparkgeo/glow/glow-exec/internal/workdir"
)

// Exit statuses of glow-exec itself. A failing tool's own status is passed
// through instead.
const (
	exitOK    = 0
	exitError = 1
	exitUsage = 2
)

const usage = `usage:
  glow-exec run --tool <name@major> [flags] [-- <command...>]
  glow-exec stage --tool <name@major> [flags]
  glow-exec collect --tool <name@major> [flags]
  glow-exec eval [--env <json>|@file] [--template] <expression>
`

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	code := Main(ctx, os.Args[1:], os.Environ(), os.Stdout, os.Stderr)
	stop()
	os.Exit(code)
}

// Main runs one glow-exec command and returns the process exit status.
func Main(ctx context.Context, args, environ []string, stdout, stderr io.Writer) int {
	slog.SetDefault(slog.New(slog.NewTextHandler(stderr, nil)).With("component", "glow-exec"))
	if len(args) == 0 {
		fmt.Fprint(stderr, usage)
		return exitUsage
	}
	var err error
	switch args[0] {
	case "run", "stage", "collect":
		err = phases(ctx, args[0], args[1:], environ, stdout, stderr)
	case "eval":
		err = evalCommand(args[1:], stdout, stderr)
	default:
		fmt.Fprint(stderr, usage)
		return exitUsage
	}
	var exitErr *run.ExitError
	var usageErr usageError
	switch {
	case err == nil:
		return exitOK
	case errors.As(err, &exitErr):
		slog.Error("tool failed", "status", exitErr.Code)
		return exitErr.Code
	case errors.As(err, &usageErr):
		fmt.Fprintf(stderr, "glow-exec: %v\n%s", err, usage)
		return exitUsage
	default:
		fmt.Fprintf(stderr, "glow-exec: error: %v\n", err)
		return exitError
	}
}

type usageError struct{ error }

// stringList is a repeatable string flag.
type stringList []string

func (l *stringList) String() string     { return strings.Join(*l, ",") }
func (l *stringList) Set(v string) error { *l = append(*l, v); return nil }

func phaseFlagSet(name string, stderr io.Writer) (*flag.FlagSet, *params.Flags, *string) {
	set := flag.NewFlagSet(name, flag.ContinueOnError)
	set.SetOutput(stderr)
	var f params.Flags
	tool := set.String("tool", "", "tool reference, name@major (required)")
	set.StringVar(&f.RawWith, "raw-with", "", "base64 JSON of the with block, or @file (overrides GLOW_RAW_WITH)")
	set.StringVar(&f.Manifest, "manifest", "", "base64 manifest, or @file (overrides GLOW_MANIFEST)")
	set.StringVar(&f.Scope, "scope", "", "JSON object of expression variables, or @file (overrides GLOW_SCOPE)")
	set.StringVar(&f.If, "if", "", "the step's if expression (overrides GLOW_IF)")
	set.Var((*stringList)(&f.Upstream), "upstream", "step=<json> or step=@file of an upstream outputs.resolved.json; repeatable")
	set.StringVar(&f.RunPrefix, "run-prefix", "", "directory or s3:// URI for outputs (overrides GLOW_RUN_PREFIX)")
	set.StringVar(&f.WorkDir, "work-dir", "", "work directory (overrides GLOW_WORK_DIR, default /work)")
	set.StringVar(&f.Staging, "staging", "", "staging mode (overrides GLOW_STAGING, default copy)")
	return set, &f, tool
}

func phases(ctx context.Context, command string, args, environ []string, stdout, stderr io.Writer) error {
	set, flags, toolRef := phaseFlagSet(command, stderr)
	if err := set.Parse(args); err != nil {
		return usageError{err}
	}
	if *toolRef == "" {
		return usageError{errors.New("--tool is required")}
	}
	if command != "run" && set.NArg() > 0 {
		return usageError{fmt.Errorf("%s takes no command", command)}
	}
	p, err := params.Load(environ, *flags)
	if err != nil {
		return err
	}
	if len(p.Manifest) == 0 {
		return errors.New("no manifest: set GLOW_MANIFEST or --manifest")
	}
	tool, err := manifest.Load(p.Manifest, *toolRef)
	if err != nil {
		return err
	}
	layout := workdir.Layout{Root: p.WorkDir}
	resolver := prefix.NewResolver()

	var inputs map[string]any
	if command != "collect" {
		result, err := stage.Run(ctx, stage.Config{Tool: tool, Params: p, Layout: layout, Resolver: resolver})
		if err != nil {
			return fmt.Errorf("stage: %w", err)
		}
		if result.Skipped || command == "stage" {
			return nil
		}
		inputs = result.Inputs
	} else if inputs, err = readInputs(layout); err != nil {
		return err
	}

	if command == "run" {
		commandLine := set.Args()
		if len(commandLine) == 0 {
			commandLine = tool.Command
		}
		if err := os.MkdirAll(layout.Out(), 0o755); err != nil {
			return err
		}
		err := run.Run(ctx, run.Config{
			Command: commandLine,
			Dir:     layout.Root,
			Env:     run.Env(environ, layout.Root),
			Stdout:  stdout,
			Stderr:  stderr,
		})
		if err != nil {
			return err
		}
	}

	_, err = collect.Run(ctx, collect.Config{
		Tool: tool, Layout: layout, RunPrefix: p.RunPrefix, Inputs: inputs, Resolver: resolver,
	})
	if err != nil {
		return fmt.Errorf("collect: %w", err)
	}
	return nil
}

// readInputs reads inputs.json for a standalone collect.
func readInputs(layout workdir.Layout) (map[string]any, error) {
	data, err := os.ReadFile(layout.InputsJSON())
	if err != nil {
		return nil, err
	}
	var inputs map[string]any
	if err := jsonvalue.Decode(data, &inputs); err != nil {
		return nil, fmt.Errorf("%s: %w", layout.InputsJSON(), err)
	}
	return inputs, nil
}

// evalCommand evaluates one expression against a JSON environment and
// prints the result as JSON. It exists to compare with the Python
// evaluator.
func evalCommand(args []string, stdout, stderr io.Writer) error {
	set := flag.NewFlagSet("eval", flag.ContinueOnError)
	set.SetOutput(stderr)
	envFlag := set.String("env", "", "JSON object whose keys are the variables, or @file")
	template := set.Bool("template", false, "treat the argument as a string with ${{ }} spans")
	if err := set.Parse(args); err != nil {
		return usageError{err}
	}
	if set.NArg() != 1 {
		return usageError{errors.New("eval takes exactly one expression")}
	}
	p, err := params.Load(nil, params.Flags{Scope: *envFlag})
	if err != nil {
		return fmt.Errorf("env: %w", err)
	}
	evaluator, err := cel.New(p.Scope)
	if err != nil {
		return err
	}
	var result any
	if *template {
		result, err = evaluator.Template(set.Arg(0))
	} else {
		result, err = evaluator.Eval(set.Arg(0))
	}
	if err != nil {
		return err
	}
	return json.NewEncoder(stdout).Encode(result)
}
