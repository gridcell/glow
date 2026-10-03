// Package stage prepares a step before its tool runs: it evaluates `if`
// and the `with` block, validates the inputs against the tool's manifest,
// downloads file inputs to /work/in/ and writes /work/inputs.json.
package stage

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"path/filepath"
	"sort"
	"strings"

	"github.com/sparkgeo/glow/glow-exec/internal/cel"
	"github.com/sparkgeo/glow/glow-exec/internal/manifest"
	"github.com/sparkgeo/glow/glow-exec/internal/mediatype"
	"github.com/sparkgeo/glow/glow-exec/internal/params"
	"github.com/sparkgeo/glow/glow-exec/internal/prefix"
	"github.com/sparkgeo/glow/glow-exec/internal/workdir"
)

// maxShownValue bounds how much of a rejected value an error message shows.
const maxShownValue = 200

// Resolver returns the store that serves a URI.
type Resolver interface {
	For(ctx context.Context, uri string) (prefix.Store, error)
}

// Config is what stage needs to prepare one step.
type Config struct {
	Tool     *manifest.Tool
	Params   *params.Params
	Layout   workdir.Layout
	Resolver Resolver
}

// Result reports what stage did.
type Result struct {
	// Skipped is true when `if` was false. The skipped marker has been
	// written and the tool must not run.
	Skipped bool
	// Inputs is the content written to inputs.json.
	Inputs map[string]any
}

// SkippedMarker is the outputs.resolved.json of a skipped step.
var SkippedMarker = map[string]any{"skipped": true}

// CheckStaging accepts the staging modes this runtime implements. `auto`
// falls back to `copy`, which is always correct; `none` needs the tool to
// read URIs directly and is not supported yet.
func CheckStaging(mode string) error {
	switch mode {
	case "", "copy", "auto":
		return nil
	default:
		return fmt.Errorf("staging mode %q is not supported; use copy", mode)
	}
}

// Run stages one step.
func Run(ctx context.Context, cfg Config) (*Result, error) {
	if err := CheckStaging(cfg.Params.Staging); err != nil {
		return nil, err
	}
	vars, err := Vars(cfg.Params.Scope, cfg.Params.Upstream)
	if err != nil {
		return nil, err
	}
	evaluator, err := cel.New(vars)
	if err != nil {
		return nil, err
	}
	run, err := evalIf(evaluator, cfg.Params.If)
	if err != nil {
		return nil, err
	}
	if !run {
		slog.Info("if is false; skipping the step")
		if err := workdir.WriteJSON(cfg.Layout.Resolved(), SkippedMarker); err != nil {
			return nil, err
		}
		return &Result{Skipped: true}, nil
	}
	with, err := evaluator.Substitute("with", cfg.Params.RawWith)
	if err != nil {
		return nil, err
	}
	inputs, err := Validate(cfg.Tool, with.(map[string]any))
	if err != nil {
		return nil, err
	}
	if err := download(ctx, cfg, inputs); err != nil {
		return nil, err
	}
	if err := workdir.WriteJSON(cfg.Layout.InputsJSON(), inputs); err != nil {
		return nil, err
	}
	return &Result{Inputs: inputs}, nil
}

// Vars builds the expression variables: each top-level key of scope, and
// `steps.<id>` from the upstream outputs. A skipped upstream step has no
// outputs. Resolved files get a `path` key equal to their `uri` so that
// expressions written as `file.path` work before the file is staged.
func Vars(scope map[string]any, upstream map[string]params.Resolved) (map[string]any, error) {
	vars := map[string]any{}
	for name, value := range scope {
		if name == "steps" {
			return nil, errors.New("scope must not define steps; pass upstream outputs instead")
		}
		vars[name] = withPathAlias(value)
	}
	steps := map[string]any{}
	for id, resolved := range upstream {
		outputs := resolved.Outputs
		if resolved.Skipped || outputs == nil {
			outputs = map[string]any{}
		}
		steps[id] = map[string]any{"outputs": withPathAlias(outputs), "skipped": resolved.Skipped}
	}
	vars["steps"] = steps
	return vars, nil
}

func withPathAlias(value any) any {
	switch v := value.(type) {
	case map[string]any:
		for key, item := range v {
			v[key] = withPathAlias(item)
		}
		uri, hasURI := v["uri"].(string)
		kind, _ := v["kind"].(string)
		if _, hasPath := v["path"]; hasURI && !hasPath && manifest.IsDataKind(kind) {
			v["path"] = uri
		}
		return v
	case []any:
		for i, item := range v {
			v[i] = withPathAlias(item)
		}
		return v
	default:
		return value
	}
}

func evalIf(evaluator *cel.Evaluator, condition string) (bool, error) {
	if strings.TrimSpace(condition) == "" {
		return true, nil
	}
	value, err := evaluator.Template(condition)
	if err != nil {
		return false, fmt.Errorf("if: %w", err)
	}
	result, ok := value.(bool)
	if !ok {
		return false, fmt.Errorf("if must evaluate to a boolean, got %s", show(value))
	}
	return result, nil
}

// Validate applies defaults and checks the resolved `with` values against
// the tool's inputs. Errors name the input and the rejected value.
func Validate(tool *manifest.Tool, with map[string]any) (map[string]any, error) {
	inputs := tool.Defaults()
	for name, value := range with {
		if value != nil {
			inputs[name] = value
		}
	}
	var problems []string
	for _, name := range sortedKeys(inputs) {
		if _, declared := tool.Inputs[name]; !declared {
			problems = append(problems, fmt.Sprintf("input %q is not declared by tool %q", name, tool.Name))
			continue
		}
		value := inputs[name]
		if err := tool.ValidateInput(name, value); err != nil {
			problems = append(problems, fmt.Sprintf("input %q value %s is invalid: %v", name, show(value), err))
			continue
		}
		if err := checkMediaTypes(tool, name, value); err != nil {
			problems = append(problems, err.Error())
		}
	}
	for _, name := range tool.Required {
		if _, ok := inputs[name]; !ok {
			problems = append(problems, fmt.Sprintf("input %q is required", name))
		}
	}
	if len(problems) > 0 {
		return nil, errors.New(strings.Join(problems, "\n"))
	}
	return inputs, nil
}

func checkMediaTypes(tool *manifest.Tool, name string, value any) error {
	declared := tool.InputMediaTypes(name)
	if len(declared) == 0 {
		return nil
	}
	refs, err := fileRefs(tool.InputType(name), value)
	if err != nil {
		return fmt.Errorf("input %q: %w", name, err)
	}
	for _, ref := range refs {
		// A bare URI carries no media type; only resolved files are checked.
		if ref.MediaType != "" && !mediatype.MatchesAny(ref.MediaType, declared) {
			return fmt.Errorf("input %q value %s has media type %q, expected one of %q",
				name, show(ref.URI), ref.MediaType, declared)
		}
	}
	return nil
}

// fileRef is one file named by an input value.
type fileRef struct {
	URI       string
	MediaType string
}

// fileRefs extracts the files of a data-kind value. The value has already
// passed schema validation, so the shapes are known.
func fileRefs(kind string, value any) ([]fileRef, error) {
	if kind != manifest.KindGroup {
		ref, err := toFileRef(value)
		return []fileRef{ref}, err
	}
	items, ok := value.([]any)
	if object, isMap := value.(map[string]any); isMap {
		items, ok = object["files"].([]any)
	}
	if !ok {
		return nil, fmt.Errorf("group value %s has no files", show(value))
	}
	refs := make([]fileRef, 0, len(items))
	for _, item := range items {
		ref, err := toFileRef(item)
		if err != nil {
			return nil, err
		}
		refs = append(refs, ref)
	}
	return refs, nil
}

func toFileRef(value any) (fileRef, error) {
	switch v := value.(type) {
	case string:
		return fileRef{URI: v}, nil
	case map[string]any:
		uri, _ := v["uri"].(string)
		mediaType, _ := v["media_type"].(string)
		if uri != "" {
			return fileRef{URI: uri, MediaType: mediaType}, nil
		}
	}
	return fileRef{}, fmt.Errorf("%s is not a file reference", show(value))
}

// download stages every data-kind input and replaces its value with the
// local path: a file path, a bundle directory, or a list of group files.
func download(ctx context.Context, cfg Config, inputs map[string]any) error {
	for _, name := range sortedKeys(inputs) {
		kind := cfg.Tool.InputType(name)
		if !manifest.IsDataKind(kind) {
			continue
		}
		dir := filepath.Join(cfg.Layout.In(), name)
		refs, err := fileRefs(kind, inputs[name])
		if err != nil {
			return fmt.Errorf("input %q: %w", name, err)
		}
		staged, err := stageInput(ctx, cfg.Resolver, kind, dir, refs)
		if err != nil {
			return fmt.Errorf("input %q: %w", name, err)
		}
		inputs[name] = staged
	}
	return nil
}

func stageInput(ctx context.Context, resolver Resolver, kind, dir string, refs []fileRef) (any, error) {
	switch kind {
	case manifest.KindBundle:
		return dir, downloadBundle(ctx, resolver, refs[0].URI, dir)
	case manifest.KindGroup:
		paths := make([]any, 0, len(refs))
		seen := map[string]bool{}
		for _, ref := range refs {
			path, err := downloadFile(ctx, resolver, ref.URI, dir)
			if err != nil {
				return nil, err
			}
			if seen[path] {
				return nil, fmt.Errorf("two group files have the basename %q", filepath.Base(path))
			}
			seen[path] = true
			paths = append(paths, path)
		}
		return paths, nil
	default:
		return downloadFile(ctx, resolver, refs[0].URI, dir)
	}
}

// downloadFile downloads uri into dir under its original basename.
func downloadFile(ctx context.Context, resolver Resolver, uri, dir string) (string, error) {
	name := basename(uri)
	if !manifest.IsLocalPath(name) || strings.Contains(name, "/") {
		return "", fmt.Errorf("%s has no usable file name", show(uri))
	}
	store, err := resolver.For(ctx, uri)
	if err != nil {
		return "", err
	}
	dest := filepath.Join(dir, name)
	slog.Info("downloading", "uri", uri, "path", dest)
	if err := store.Download(ctx, uri, dest); err != nil {
		return "", fmt.Errorf("downloading %s: %w", uri, err)
	}
	return dest, nil
}

// downloadBundle downloads every object under uri into dir, keeping the
// relative layout. Object names that would leave dir are refused.
func downloadBundle(ctx context.Context, resolver Resolver, uri, dir string) error {
	store, err := resolver.For(ctx, uri)
	if err != nil {
		return err
	}
	paths, err := store.List(ctx, uri)
	if err != nil {
		return err
	}
	if len(paths) == 0 {
		return fmt.Errorf("bundle %s is empty", show(uri))
	}
	slog.Info("downloading bundle", "uri", uri, "files", len(paths), "path", dir)
	for _, rel := range paths {
		if !manifest.IsLocalPath(rel) {
			return fmt.Errorf("bundle %s contains the unsafe name %q", show(uri), rel)
		}
		if err := store.Download(ctx, prefix.Join(uri, rel), filepath.Join(dir, filepath.FromSlash(rel))); err != nil {
			return err
		}
	}
	return nil
}

func basename(uri string) string {
	trimmed := strings.TrimRight(uri, "/")
	return trimmed[strings.LastIndex(trimmed, "/")+1:]
}

func sortedKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for key := range m {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// show renders a value for an error message, truncated.
func show(value any) string {
	data, err := json.Marshal(value)
	if err != nil {
		return fmt.Sprintf("%v", value)
	}
	if len(data) > maxShownValue {
		return string(data[:maxShownValue]) + "..."
	}
	return string(data)
}
