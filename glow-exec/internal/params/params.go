// Package params decodes the step parameters that the compiler passes to
// glow-exec. Each one comes from an environment variable, which a command
// line flag of the same meaning overrides:
//
//	GLOW_RAW_WITH        --raw-with   base64 JSON of the step's `with` block
//	GLOW_MANIFEST        --manifest   base64 toolpack manifest or tool spec (YAML or JSON)
//	GLOW_SCOPE           --scope      JSON object of expression variables
//	GLOW_LET             --let        base64 JSON list of let bindings, evaluated in order
//	GLOW_IF              --if         the step's `if` expression, `${{ ... }}`
//	GLOW_UPSTREAM_<id>   --upstream   JSON of step <id>'s outputs.resolved.json
//	GLOW_RUN_PREFIX      --run-prefix where outputs are uploaded
//	GLOW_WORK_DIR        --work-dir   work directory, /work by default
//	GLOW_STAGING         --staging    staging mode, copy by default
//
// A flag value that starts with "@" names a file whose content is used
// without base64 encoding, for example `--manifest @manifest.yaml`.
package params

import (
	"bytes"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"

	"github.com/sparkgeo/glow/glow-exec/internal/jsonvalue"
	"github.com/sparkgeo/glow/glow-exec/internal/workdir"
)

// MaxSize bounds one decoded parameter.
const MaxSize = 16 << 20

const upstreamEnvPrefix = "GLOW_UPSTREAM_"

var stepID = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,62}$`)

var letName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// Flags are the raw command line values. Empty means "not given".
type Flags struct {
	RawWith   string
	Manifest  string
	Scope     string
	Let       string
	If        string
	Upstream  []string // id=<json> or id=@file
	RunPrefix string
	WorkDir   string
	Staging   string
}

// Resolved is the content of a step's outputs.resolved.json.
type Resolved struct {
	Outputs map[string]any `json:"outputs"`
	Skipped bool           `json:"skipped"`
}

// Binding is one `let`: a name and its value as written, with `${{ }}`
// expressions unevaluated.
type Binding struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// Params are the decoded step parameters.
type Params struct {
	RawWith   map[string]any
	Manifest  []byte
	Scope     map[string]any
	Let       []Binding
	If        string
	Upstream  map[string]Resolved
	RunPrefix string
	WorkDir   string
	Staging   string
}

// Load decodes the parameters from the environment, given as KEY=VALUE
// entries, and the flags.
func Load(environ []string, flags Flags) (*Params, error) {
	env := map[string]string{}
	for _, entry := range environ {
		key, value, _ := strings.Cut(entry, "=")
		env[key] = value
	}
	pick := func(flag, key string) string {
		if flag != "" {
			return flag
		}
		return env[key]
	}
	p := &Params{
		If:        pick(flags.If, "GLOW_IF"),
		RunPrefix: pick(flags.RunPrefix, "GLOW_RUN_PREFIX"),
		WorkDir:   pick(flags.WorkDir, "GLOW_WORK_DIR"),
		Staging:   pick(flags.Staging, "GLOW_STAGING"),
	}
	if p.WorkDir == "" {
		p.WorkDir = workdir.Default
	}
	var err error
	if p.Manifest, err = encoded(pick(flags.Manifest, "GLOW_MANIFEST")); err != nil {
		return nil, fmt.Errorf("manifest: %w", err)
	}
	rawWith, err := encoded(pick(flags.RawWith, "GLOW_RAW_WITH"))
	if err != nil {
		return nil, fmt.Errorf("raw-with: %w", err)
	}
	if p.RawWith, err = jsonObject(rawWith); err != nil {
		return nil, fmt.Errorf("raw-with: %w", err)
	}
	scope, err := plain(pick(flags.Scope, "GLOW_SCOPE"))
	if err != nil {
		return nil, fmt.Errorf("scope: %w", err)
	}
	if p.Scope, err = jsonObject(scope); err != nil {
		return nil, fmt.Errorf("scope: %w", err)
	}
	lets, err := encoded(pick(flags.Let, "GLOW_LET"))
	if err != nil {
		return nil, fmt.Errorf("let: %w", err)
	}
	if p.Let, err = bindings(lets); err != nil {
		return nil, fmt.Errorf("let: %w", err)
	}
	if p.Upstream, err = upstream(env, flags.Upstream); err != nil {
		return nil, err
	}
	return p, nil
}

// encoded decodes a base64 parameter, or reads an @file as is.
func encoded(value string) ([]byte, error) {
	if path, ok := strings.CutPrefix(value, "@"); ok {
		return readFile(path)
	}
	if len(value) > base64.StdEncoding.EncodedLen(MaxSize) {
		return nil, fmt.Errorf("larger than %d bytes", MaxSize)
	}
	data, err := base64.StdEncoding.DecodeString(strings.TrimSpace(value))
	if err != nil {
		return nil, errors.New("not valid base64")
	}
	return data, nil
}

// plain returns a JSON parameter, or reads an @file.
func plain(value string) ([]byte, error) {
	if path, ok := strings.CutPrefix(value, "@"); ok {
		return readFile(path)
	}
	if len(value) > MaxSize {
		return nil, fmt.Errorf("larger than %d bytes", MaxSize)
	}
	return []byte(value), nil
}

func readFile(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, MaxSize+1))
	if err != nil {
		return nil, err
	}
	if len(data) > MaxSize {
		return nil, fmt.Errorf("%s is larger than %d bytes", path, MaxSize)
	}
	return data, nil
}

// jsonObject decodes a JSON object; empty input is an empty object.
func jsonObject(data []byte) (map[string]any, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return map[string]any{}, nil
	}
	object, err := jsonvalue.DecodeObject(data)
	if err != nil {
		return nil, fmt.Errorf("not a valid JSON object: %w", err)
	}
	return object, nil
}

// bindings decodes a JSON list of let bindings; empty input is no bindings.
func bindings(data []byte) ([]Binding, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return nil, nil
	}
	var lets []Binding
	if err := jsonvalue.Decode(data, &lets); err != nil {
		return nil, fmt.Errorf("not a valid JSON list of {name, value} objects: %w", err)
	}
	seen := map[string]bool{}
	for _, let := range lets {
		if !letName.MatchString(let.Name) {
			return nil, fmt.Errorf("name %q is not an identifier", let.Name)
		}
		if seen[let.Name] {
			return nil, fmt.Errorf("%s is bound twice", let.Name)
		}
		seen[let.Name] = true
	}
	return lets, nil
}

func upstream(env map[string]string, flags []string) (map[string]Resolved, error) {
	raw := map[string]string{}
	for key, value := range env {
		if id, ok := strings.CutPrefix(key, upstreamEnvPrefix); ok {
			raw[id] = value
		}
	}
	for _, flag := range flags {
		id, value, found := strings.Cut(flag, "=")
		if !found {
			return nil, fmt.Errorf("upstream %q must be <step>=<json> or <step>=@file", flag)
		}
		raw[id] = value
	}
	out := map[string]Resolved{}
	for id, value := range raw {
		if !stepID.MatchString(id) {
			return nil, fmt.Errorf("upstream step id %q is not an identifier", id)
		}
		data, err := plain(value)
		if err != nil {
			return nil, fmt.Errorf("upstream %s: %w", id, err)
		}
		object, err := jsonObject(data)
		if err != nil {
			return nil, fmt.Errorf("upstream %s: %w", id, err)
		}
		resolved, err := toResolved(object)
		if err != nil {
			return nil, fmt.Errorf("upstream %s: %w", id, err)
		}
		out[id] = resolved
	}
	return out, nil
}

func toResolved(object map[string]any) (Resolved, error) {
	var resolved Resolved
	if skipped, ok := object["skipped"]; ok {
		flag, isBool := skipped.(bool)
		if !isBool {
			return resolved, errors.New("skipped must be a boolean")
		}
		resolved.Skipped = flag
	}
	resolved.Outputs = map[string]any{}
	if outputs, ok := object["outputs"]; ok && outputs != nil {
		values, isMap := outputs.(map[string]any)
		if !isMap {
			return resolved, errors.New("outputs must be an object")
		}
		resolved.Outputs = values
	}
	return resolved, nil
}
