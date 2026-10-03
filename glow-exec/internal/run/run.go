// Package run executes a step's tool as a child process.
package run

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

// defaultPath is used when the parent has no PATH, as in a scratch-based
// init container.
const defaultPath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

// stopGrace is how long a tool may take to exit after SIGTERM before it is
// killed.
const stopGrace = 10 * time.Second

// passThrough are the parent variables a tool may see. Everything else,
// cloud credentials in particular, stays with glow-exec: the tool reads its
// inputs from /work and needs no access to the run prefix.
var passThrough = []string{"PATH", "TMPDIR", "LANG", "LC_ALL", "TZ"}

// ExitError reports a tool that exited with a non-zero status.
type ExitError struct {
	Code int
}

func (e *ExitError) Error() string {
	return fmt.Sprintf("tool exited with status %d", e.Code)
}

// Config describes one tool execution.
type Config struct {
	Command []string
	Dir     string
	Env     []string
	Stdout  io.Writer
	Stderr  io.Writer
}

// Env returns the minimal environment for a tool: the pass-through
// variables from parent, HOME and GLOW_WORK_DIR set to the work directory.
func Env(parent []string, workDir string) []string {
	values := map[string]string{}
	for _, entry := range parent {
		key, value, _ := strings.Cut(entry, "=")
		values[key] = value
	}
	if values["PATH"] == "" {
		values["PATH"] = defaultPath
	}
	env := []string{"HOME=" + workDir, "GLOW_WORK_DIR=" + workDir}
	for _, key := range passThrough {
		if value, ok := values[key]; ok && value != "" {
			env = append(env, key+"="+value)
		}
	}
	return env
}

// Run starts the command in cfg.Dir and waits for it, streaming its stdout
// and stderr. The command is passed to exec directly, never to a shell.
// When ctx is cancelled the tool gets SIGTERM, then SIGKILL after a grace
// period.
func Run(ctx context.Context, cfg Config) error {
	if len(cfg.Command) == 0 {
		return errors.New("no command to run")
	}
	cmd := exec.CommandContext(ctx, cfg.Command[0], cfg.Command[1:]...)
	cmd.Dir = cfg.Dir
	cmd.Env = cfg.Env
	cmd.Stdout = cfg.Stdout
	cmd.Stderr = cfg.Stderr
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = stopGrace
	slog.Info("running tool", "command", cfg.Command[0])
	err := cmd.Run()
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) && exitErr.ExitCode() > 0 {
		return &ExitError{Code: exitErr.ExitCode()}
	}
	if err != nil {
		return fmt.Errorf("running %s: %w", cfg.Command[0], err)
	}
	return nil
}
