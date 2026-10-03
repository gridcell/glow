package manifest

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func loadRepoTool(t *testing.T, toolpack, ref string) *Tool {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "toolpacks", toolpack, "manifest.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	tool, err := Load(data, ref)
	if err != nil {
		t.Fatal(err)
	}
	return tool
}

func TestLoadsRepositoryManifests(t *testing.T) {
	paths, err := filepath.Glob(filepath.Join("..", "..", "..", "toolpacks", "*", "manifest.yaml"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no manifests found: %v", err)
	}
	for _, path := range paths {
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var pack toolpack
		doc, err := toJSON(data)
		if err != nil {
			t.Fatal(err)
		}
		if err := jsonUnmarshal(doc, &pack); err != nil {
			t.Fatal(err)
		}
		for _, tool := range pack.Tools {
			if _, err := Load(data, tool.Name+"@1"); err != nil {
				t.Errorf("%s: %v", path, err)
			}
		}
	}
}

func TestLoadSelectsByMajor(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "toolpacks", "gdal", "manifest.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	for ref, want := range map[string]string{
		"gdal.translate@2": "is version 1, not 2",
		"gdal.nope@1":      `has no tool "gdal.nope"`,
		"gdal.translate":   "must be name@major",
		"gdal.translate@x": "invalid major",
	} {
		if _, err := Load(data, ref); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("Load(%q) error = %v, want %q", ref, err, want)
		}
	}
}

func TestLoadSingleToolSpec(t *testing.T) {
	spec := []byte(`{"name": "script.count", "inputs": {"n": {"type": "integer"}}, "outputs": {"total": {"type": "integer"}}}`)
	tool, err := Load(spec, "script.count@1")
	if err != nil {
		t.Fatal(err)
	}
	if tool.InputType("n") != "integer" {
		t.Fatalf("unexpected tool %+v", tool)
	}
	if _, err := Load(spec, "script.other@1"); err == nil {
		t.Fatal("a mismatched name should fail")
	}
}

func TestLoadRejectsUnsafeNames(t *testing.T) {
	for _, spec := range []string{
		`{"name": "a.b", "inputs": {"../x": {"type": "string"}}}`,
		`{"name": "a.b", "outputs": {"../x": {"type": "string"}}}`,
		`{"name": "a.b", "outputs": {"x": {"type": "file", "path": "../x.tif"}}}`,
		`{"name": "a.b", "outputs": {"x": {"type": "file", "path": "/etc/x"}}}`,
		`{"name": "a.b", "required": ["x"]}`,
		`[1, 2]`,
	} {
		if _, err := Load([]byte(spec), "a.b@1"); err == nil {
			t.Errorf("Load(%s) should fail", spec)
		}
	}
}

func TestValidateInput(t *testing.T) {
	tool := loadRepoTool(t, "gdal", "gdal.translate@1")
	valid := map[string]any{
		"source":           map[string]any{"uri": "s3://b/a.nc", "media_type": "application/x-netcdf", "kind": "file", "path": "s3://b/a.nc"},
		"format":           "COG",
		"creation_options": map[string]any{"COMPRESS": "DEFLATE"},
	}
	for name, value := range valid {
		if err := tool.ValidateInput(name, value); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
	invalid := map[string]any{
		"source":           int64(3),
		"format":           "JPEG",
		"creation_options": map[string]any{"PREDICTOR": int64(2)},
	}
	for name, value := range invalid {
		if err := tool.ValidateInput(name, value); err == nil {
			t.Errorf("%s=%v should be invalid", name, value)
		}
	}
	relief := loadRepoTool(t, "gdal", "gdal.dem.color_relief@1")
	if err := relief.ValidateInput("size", []any{int64(600)}); err == nil || !strings.Contains(err.Error(), "minItems") {
		t.Errorf("size error = %v, want minItems", err)
	}
}

func TestOutputMediaType(t *testing.T) {
	tool := loadRepoTool(t, "gdal", "gdal.translate@1")
	got, err := tool.OutputMediaType("result", map[string]any{"format": "PNG"})
	if err != nil || got != "image/png" {
		t.Fatalf("got %q, %v", got, err)
	}
	if _, err := tool.OutputMediaType("result", map[string]any{}); err == nil {
		t.Fatal("a missing media_type_from input should fail")
	}
	if tool.Defaults()["format"] != "COG" {
		t.Fatalf("defaults = %v", tool.Defaults())
	}
}

func TestIsLocalPath(t *testing.T) {
	for p, want := range map[string]bool{
		"out.tif": true, "a/b.tif": true, "./a": true,
		"": false, ".": false, "..": false, "a/../../b": false, "/abs": false, `a\b`: false,
	} {
		if got := IsLocalPath(p); got != want {
			t.Errorf("IsLocalPath(%q) = %v, want %v", p, got, want)
		}
	}
}

func jsonUnmarshal(data []byte, into any) error { return json.Unmarshal(data, into) }
