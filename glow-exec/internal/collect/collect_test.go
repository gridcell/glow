package collect

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/sparkgeo/glow/glow-exec/internal/manifest"
	"github.com/sparkgeo/glow/glow-exec/internal/prefix"
	"github.com/sparkgeo/glow/glow-exec/internal/workdir"
)

const spec = `
name: t.collect
inputs:
  format: { type: string, enum: [PNG, TIF, RAW], default: PNG }
outputs:
  image:
    type: file
    path: img/out.{ext}
    media_type_from: format
    media_types: { PNG: image/png, TIF: image/tiff, RAW: application/x-raw }
  tiles: { type: bundle, path: tiles }
  parts: { type: group }
  info: { type: object }
`

type fixture struct {
	cfg    Config
	layout workdir.Layout
}

func setup(t *testing.T, format string) fixture {
	t.Helper()
	tool, err := manifest.Load([]byte(spec), "t.collect@1")
	if err != nil {
		t.Fatal(err)
	}
	layout := workdir.Layout{Root: t.TempDir()}
	f := fixture{layout: layout, cfg: Config{
		Tool:      tool,
		Layout:    layout,
		RunPrefix: filepath.Join(t.TempDir(), "runs", "s1"),
		Inputs:    map[string]any{"format": format},
		Resolver:  prefix.NewResolver(),
	}}
	f.write(t, "out/img/out.png", "png")
	f.write(t, "out/tiles/0/0.png", "t")
	f.write(t, "out/tiles/0/1.png", "t")
	f.write(t, "out/p1.csv", "1")
	f.write(t, "out/p2.csv", "2")
	f.write(t, "outputs.json", `{"parts": ["p1.csv", "p2.csv"], "info": {"bands": 3}}`)
	return f
}

func (f fixture) write(t *testing.T, rel, content string) {
	t.Helper()
	path := filepath.Join(f.layout.Root, rel)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestCollectEveryKind(t *testing.T) {
	f := setup(t, "PNG")
	resolved, err := Run(context.Background(), f.cfg)
	if err != nil {
		t.Fatal(err)
	}
	outputs := resolved["outputs"].(map[string]any)
	image := outputs["image"].(map[string]any)
	if image["uri"] != prefix.Join(f.cfg.RunPrefix, "image", "out.png") || image["media_type"] != "image/png" {
		t.Fatalf("image = %v", image)
	}
	if _, err := os.Stat(prefix.Join(f.cfg.RunPrefix, "tiles", "0", "1.png")); err != nil {
		t.Fatalf("bundle not uploaded recursively: %v", err)
	}
	parts := outputs["parts"].(map[string]any)
	files := parts["files"].([]any)
	if len(files) != 2 || files[0].(map[string]any)["media_type"] != "text/csv" {
		t.Fatalf("parts = %v", parts)
	}
	if outputs["info"].(map[string]any)["bands"] != int64(3) {
		t.Fatalf("info = %v", outputs["info"])
	}
	if _, err := os.Stat(f.layout.Resolved()); err != nil {
		t.Fatal(err)
	}
}

func TestCollectErrors(t *testing.T) {
	cases := map[string]struct {
		format  string
		mutate  func(t *testing.T, f fixture)
		message string
	}{
		"extension mismatch": {"TIF", func(t *testing.T, f fixture) { f.write(t, "out/img/out.tif", "x") }, ""},
		"missing fixed path": {"TIF", nil, "img/out.tif, not found"},
		"unknown extension":  {"RAW", nil, `no extension known for media type "application/x-raw"`},
		"undeclared output": {"PNG", func(t *testing.T, f fixture) {
			f.write(t, "outputs.json", `{"parts": ["p1.csv"], "info": {}, "bogus": 1}`)
		}, `"bogus", which tool`},
		"wrong value type": {"PNG", func(t *testing.T, f fixture) {
			f.write(t, "outputs.json", `{"parts": ["p1.csv"], "info": 3}`)
		}, `output "info"`},
		"empty bundle": {"PNG", func(t *testing.T, f fixture) {
			os.RemoveAll(filepath.Join(f.layout.Out(), "tiles"))
			os.MkdirAll(filepath.Join(f.layout.Out(), "tiles"), 0o755)
		}, "is empty"},
		"bundle symlink": {"PNG", func(t *testing.T, f fixture) {
			os.Symlink("/etc/hostname", filepath.Join(f.layout.Out(), "tiles", "link"))
		}, "not a regular file"},
		"group path escape": {"PNG", func(t *testing.T, f fixture) {
			f.write(t, "outputs.json", `{"parts": ["/etc/hostname"], "info": {}}`)
		}, "must stay under"},
		"invalid outputs.json": {"PNG", func(t *testing.T, f fixture) { f.write(t, "outputs.json", `[1]`) }, "JSON object"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			f := setup(t, c.format)
			if c.mutate != nil {
				c.mutate(t, f)
			}
			if name == "extension mismatch" {
				f.cfg.Tool.Outputs["image"] = manifest.Output{Type: "file", Path: "img/out.png", MediaType: "image/tiff"}
				c.message = "does not have an extension"
			}
			_, err := Run(context.Background(), f.cfg)
			if err == nil || !strings.Contains(err.Error(), c.message) {
				t.Fatalf("error = %v, want it to contain %q", err, c.message)
			}
		})
	}
}

func TestRunPrefixIsRequired(t *testing.T) {
	f := setup(t, "PNG")
	f.cfg.RunPrefix = ""
	if _, err := Run(context.Background(), f.cfg); err == nil {
		t.Fatal("expected an error without a run prefix")
	}
	f.cfg.RunPrefix = "http://example.com/x"
	if _, err := Run(context.Background(), f.cfg); err == nil {
		t.Fatal("expected an error for an http run prefix")
	}
}
