// Package collect gathers a step's outputs after its tool exits: it checks
// every declared output, uploads files to the run prefix and writes
// /work/outputs.resolved.json and /work/outputs/<name>.json.
package collect

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/sparkgeo/glow/glow-exec/internal/jsonvalue"
	"github.com/sparkgeo/glow/glow-exec/internal/manifest"
	"github.com/sparkgeo/glow/glow-exec/internal/mediatype"
	"github.com/sparkgeo/glow/glow-exec/internal/params"
	"github.com/sparkgeo/glow/glow-exec/internal/prefix"
	"github.com/sparkgeo/glow/glow-exec/internal/workdir"
)

// fallbackMediaType is used for a file output whose manifest names no
// media type and whose extension is unknown.
const fallbackMediaType = "application/octet-stream"

// Resolver returns the store that serves a URI.
type Resolver interface {
	For(ctx context.Context, uri string) (prefix.Store, error)
}

// Config is what collect needs for one step.
type Config struct {
	Tool      *manifest.Tool
	Layout    workdir.Layout
	RunPrefix string
	// Inputs are the staged inputs; media_type_from reads them.
	Inputs   map[string]any
	Resolver Resolver
}

// Run collects the outputs and returns the outputs.resolved.json content.
func Run(ctx context.Context, cfg Config) (map[string]any, error) {
	if cfg.RunPrefix == "" {
		return nil, errors.New("GLOW_RUN_PREFIX is not set")
	}
	store, err := cfg.Resolver.For(ctx, cfg.RunPrefix)
	if err != nil {
		return nil, fmt.Errorf("run prefix: %w", err)
	}
	reported, err := readReported(cfg.Layout.OutputsJSON())
	if err != nil {
		return nil, err
	}
	for name := range reported {
		if _, ok := cfg.Tool.Outputs[name]; !ok {
			return nil, fmt.Errorf("%s sets %q, which tool %q does not declare", cfg.Layout.OutputsJSON(), name, cfg.Tool.Name)
		}
	}
	c := collector{cfg: cfg, store: store, reported: reported}
	outputs := map[string]any{}
	for _, name := range sortedNames(cfg.Tool.Outputs) {
		value, err := c.output(ctx, name)
		if err != nil {
			return nil, err
		}
		outputs[name] = value
	}
	for name, value := range outputs {
		if err := workdir.WriteJSON(cfg.Layout.Output(name), value); err != nil {
			return nil, err
		}
	}
	resolved := map[string]any{"outputs": outputs, "skipped": false}
	if err := workdir.WriteJSON(cfg.Layout.Resolved(), resolved); err != nil {
		return nil, err
	}
	return resolved, nil
}

// readReported reads the tool's outputs.json. A missing file means the tool
// reported no values.
func readReported(path string) (map[string]any, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return map[string]any{}, nil
	}
	if err != nil {
		return nil, err
	}
	if len(data) > params.MaxSize {
		return nil, fmt.Errorf("%s is larger than %d bytes", path, params.MaxSize)
	}
	object, err := jsonvalue.DecodeObject(data)
	if err != nil {
		return nil, fmt.Errorf("%s must hold a JSON object: %w", path, err)
	}
	return object, nil
}

type collector struct {
	cfg      Config
	store    prefix.Store
	reported map[string]any
}

func (c collector) output(ctx context.Context, name string) (any, error) {
	decl := c.cfg.Tool.Outputs[name]
	if !manifest.IsDataKind(decl.Type) {
		return c.value(name)
	}
	mediaType, err := c.cfg.Tool.OutputMediaType(name, c.cfg.Inputs)
	if err != nil {
		return nil, err
	}
	paths, err := c.locate(name, decl, mediaType)
	if err != nil {
		return nil, err
	}
	switch decl.Type {
	case manifest.KindBundle:
		return c.uploadBundle(ctx, name, paths[0], mediaType)
	case manifest.KindGroup:
		return c.uploadGroup(ctx, name, paths, mediaType)
	default:
		return c.uploadFile(ctx, name, paths[0], mediaType)
	}
}

// value returns a non-file output from outputs.json.
func (c collector) value(name string) (any, error) {
	value, ok := c.reported[name]
	if !ok {
		return nil, fmt.Errorf("output %q: expected key %q in %s, not found", name, name, c.cfg.Layout.OutputsJSON())
	}
	if err := c.cfg.Tool.ValidateOutput(name, value); err != nil {
		return nil, fmt.Errorf("output %q: %v", name, err)
	}
	return value, nil
}

// locate returns the local paths of a data-kind output: the manifest's
// fixed path, or the path or paths the tool wrote to outputs.json.
func (c collector) locate(name string, decl manifest.Output, mediaType string) ([]string, error) {
	var rels []string
	switch {
	case decl.Path != "":
		rel, err := expandExt(name, decl.Path, mediaType)
		if err != nil {
			return nil, err
		}
		rels = []string{rel}
	default:
		reported, ok := c.reported[name]
		if !ok {
			return nil, fmt.Errorf("output %q: no path in the manifest and no key %q in %s", name, name, c.cfg.Layout.OutputsJSON())
		}
		var err error
		if rels, err = reportedPaths(name, decl.Type, reported); err != nil {
			return nil, err
		}
	}
	paths := make([]string, 0, len(rels))
	for _, rel := range rels {
		path, err := c.existing(name, decl.Type, rel)
		if err != nil {
			return nil, err
		}
		paths = append(paths, path)
	}
	return paths, nil
}

func expandExt(name, path, mediaType string) (string, error) {
	if !strings.Contains(path, "{ext}") {
		return path, nil
	}
	ext := mediatype.Ext(mediaType)
	if ext == "" {
		return "", fmt.Errorf("output %q: no extension known for media type %q to expand %s", name, mediaType, path)
	}
	return strings.ReplaceAll(path, "{ext}", ext), nil
}

func reportedPaths(name, kind string, reported any) ([]string, error) {
	if text, ok := reported.(string); ok && kind != manifest.KindGroup {
		return []string{text}, nil
	}
	items, ok := reported.([]any)
	if !ok || kind != manifest.KindGroup {
		return nil, fmt.Errorf("output %q: outputs.json must give a path for a %s output", name, kind)
	}
	paths := make([]string, 0, len(items))
	for _, item := range items {
		text, ok := item.(string)
		if !ok {
			return nil, fmt.Errorf("output %q: group entries must be paths", name)
		}
		paths = append(paths, text)
	}
	return paths, nil
}

// existing resolves rel against /work/out/ and checks that it exists with
// the right type and stays inside /work/out/, symbolic links included.
func (c collector) existing(name, kind, rel string) (string, error) {
	outDir := c.cfg.Layout.Out()
	if filepath.IsAbs(rel) {
		inside, err := filepath.Rel(outDir, rel)
		if err != nil {
			return "", fmt.Errorf("output %q: path %s is not under %s", name, rel, outDir)
		}
		rel = filepath.ToSlash(inside)
	}
	if !manifest.IsLocalPath(rel) {
		return "", fmt.Errorf("output %q: path %q must stay under %s", name, rel, outDir)
	}
	expected := filepath.Join(outDir, filepath.FromSlash(rel))
	real, err := filepath.EvalSymlinks(expected)
	if err != nil {
		return "", fmt.Errorf("output %q: expected %s at %s, not found", name, kind, expected)
	}
	realOut, err := filepath.EvalSymlinks(outDir)
	if err != nil {
		return "", err
	}
	if inside, err := filepath.Rel(realOut, real); err != nil || !manifest.IsLocalPath(filepath.ToSlash(inside)) {
		return "", fmt.Errorf("output %q: %s points outside %s", name, expected, outDir)
	}
	info, err := os.Stat(real)
	if err != nil {
		return "", err
	}
	wantDir := kind == manifest.KindBundle
	if info.IsDir() != wantDir || (!wantDir && !info.Mode().IsRegular()) {
		return "", fmt.Errorf("output %q: expected %s at %s, found something else", name, kind, expected)
	}
	// A bundle is walked from its real directory, because WalkDir does not
	// follow a symbolic link at the root. A file keeps its declared name.
	if wantDir {
		return real, nil
	}
	return expected, nil
}

// fileMediaType checks a file against the output's media type, or derives
// one from its extension when the manifest names none.
func fileMediaType(name, path, mediaType string) (string, error) {
	if mediaType == "" {
		if base := mediatype.BaseForPath(path); base != "" {
			return base, nil
		}
		return fallbackMediaType, nil
	}
	if !mediatype.ConsistentWithPath(path, mediaType) {
		return "", fmt.Errorf("output %q: file %s does not have an extension for media type %q",
			name, filepath.Base(path), mediaType)
	}
	return mediaType, nil
}

func (c collector) uploadFile(ctx context.Context, name, path, mediaType string) (map[string]any, error) {
	mediaType, err := fileMediaType(name, path, mediaType)
	if err != nil {
		return nil, err
	}
	uri := prefix.Join(c.cfg.RunPrefix, name, filepath.Base(path))
	slog.Info("uploading", "output", name, "uri", uri)
	if err := c.store.Upload(ctx, path, uri, mediaType); err != nil {
		return nil, fmt.Errorf("output %q: %w", name, err)
	}
	return map[string]any{"uri": uri, "media_type": mediaType, "kind": manifest.KindFile}, nil
}

func (c collector) uploadGroup(ctx context.Context, name string, paths []string, mediaType string) (map[string]any, error) {
	files := make([]any, 0, len(paths))
	seen := map[string]bool{}
	for _, path := range paths {
		base := filepath.Base(path)
		if seen[base] {
			return nil, fmt.Errorf("output %q: two files have the basename %q", name, base)
		}
		seen[base] = true
		file, err := c.uploadFile(ctx, name, path, mediaType)
		if err != nil {
			return nil, err
		}
		files = append(files, file)
	}
	group := map[string]any{"uri": prefix.Join(c.cfg.RunPrefix, name), "kind": manifest.KindGroup, "files": files}
	if mediaType != "" {
		group["media_type"] = mediaType
	}
	return group, nil
}

// uploadBundle uploads every regular file under dir, keeping the layout.
// Symbolic links are refused so that a bundle cannot pull in files from
// outside /work/out/.
func (c collector) uploadBundle(ctx context.Context, name, dir, mediaType string) (map[string]any, error) {
	uri := prefix.Join(c.cfg.RunPrefix, name)
	count := 0
	err := filepath.WalkDir(dir, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return err
		}
		if !entry.Type().IsRegular() {
			return fmt.Errorf("output %q: %s is not a regular file", name, path)
		}
		rel, err := filepath.Rel(dir, path)
		if err != nil {
			return err
		}
		count++
		return c.store.Upload(ctx, path, prefix.Join(uri, filepath.ToSlash(rel)), "")
	})
	if err != nil {
		return nil, err
	}
	if count == 0 {
		return nil, fmt.Errorf("output %q: bundle directory %s is empty", name, dir)
	}
	slog.Info("uploaded bundle", "output", name, "uri", uri, "files", count)
	bundle := map[string]any{"uri": uri, "kind": manifest.KindBundle}
	if mediaType != "" {
		bundle["media_type"] = mediaType
	}
	return bundle, nil
}

func sortedNames(outputs map[string]manifest.Output) []string {
	names := make([]string, 0, len(outputs))
	for name := range outputs {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}
