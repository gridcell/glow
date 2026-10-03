package stage

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/sparkgeo/glow/glow-exec/internal/manifest"
	"github.com/sparkgeo/glow/glow-exec/internal/params"
	"github.com/sparkgeo/glow/glow-exec/internal/prefix"
	"github.com/sparkgeo/glow/glow-exec/internal/workdir"
)

const spec = `
name: t.stage
inputs:
  one: { type: file, media_type: text/plain }
  many: { type: group }
  dir: { type: bundle }
  count: { type: integer, default: 2 }
  mode: { type: string, enum: [a, b] }
required: [one]
`

func tool(t *testing.T) *manifest.Tool {
	t.Helper()
	tl, err := manifest.Load([]byte(spec), "t.stage@1")
	if err != nil {
		t.Fatal(err)
	}
	return tl
}

func write(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestValidateAppliesDefaultsAndReportsEveryProblem(t *testing.T) {
	inputs, err := Validate(tool(t), map[string]any{"one": "/a.txt", "mode": nil})
	if err != nil {
		t.Fatal(err)
	}
	if want := map[string]any{"one": "/a.txt", "count": int64(2)}; !reflect.DeepEqual(inputs, want) {
		t.Fatalf("inputs = %#v, want %#v", inputs, want)
	}
	_, err = Validate(tool(t), map[string]any{"mode": "c", "extra": true})
	for _, want := range []string{`input "extra" is not declared`, `input "mode" value "c"`, `input "one" is required`} {
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("error = %v, want it to contain %s", err, want)
		}
	}
}

func TestValidateMediaTypes(t *testing.T) {
	file := map[string]any{"uri": "/a.png", "media_type": "image/png", "kind": "file"}
	if _, err := Validate(tool(t), map[string]any{"one": file}); err == nil || !strings.Contains(err.Error(), `input "one"`) {
		t.Fatalf("error = %v, want a media type error for one", err)
	}
	file["media_type"] = "text/plain; charset=utf-8"
	if _, err := Validate(tool(t), map[string]any{"one": file}); err != nil {
		t.Fatal(err)
	}
}

func TestVars(t *testing.T) {
	vars, err := Vars(map[string]any{"g": map[string]any{"files": []any{
		map[string]any{"uri": "s3://b/a.nc", "kind": "file"},
	}}}, map[string]params.Resolved{
		"a": {Outputs: map[string]any{"n": int64(1)}},
		"s": {Outputs: map[string]any{"n": int64(1)}, Skipped: true},
	})
	if err != nil {
		t.Fatal(err)
	}
	file := vars["g"].(map[string]any)["files"].([]any)[0].(map[string]any)
	if file["path"] != "s3://b/a.nc" {
		t.Fatalf("path alias missing: %v", file)
	}
	steps := vars["steps"].(map[string]any)
	if len(steps["s"].(map[string]any)["outputs"].(map[string]any)) != 0 {
		t.Fatalf("a skipped step must have no outputs: %v", steps["s"])
	}
	if _, err := Vars(map[string]any{"steps": 1}, nil); err == nil {
		t.Fatal("scope must not define steps")
	}
}

func TestRunStagesFilesGroupsAndBundles(t *testing.T) {
	src := t.TempDir()
	write(t, filepath.Join(src, "one.txt"), "1")
	write(t, filepath.Join(src, "g1", "x.nc"), "x")
	write(t, filepath.Join(src, "g2", "y.nc"), "y")
	write(t, filepath.Join(src, "bundle", "a.json"), "a")
	write(t, filepath.Join(src, "bundle", "sub", "b.json"), "b")
	work := t.TempDir()
	layout := workdir.Layout{Root: work}
	p := &params.Params{
		RawWith: map[string]any{
			"one":  filepath.Join(src, "one.txt"),
			"many": map[string]any{"files": []any{"file://" + filepath.Join(src, "g1", "x.nc"), filepath.Join(src, "g2", "y.nc")}},
			"dir":  map[string]any{"uri": filepath.Join(src, "bundle"), "kind": "bundle"},
		},
		Scope: map[string]any{},
	}
	result, err := Run(context.Background(), Config{Tool: tool(t), Params: p, Layout: layout, Resolver: prefix.NewResolver()})
	if err != nil {
		t.Fatal(err)
	}
	in := layout.In()
	want := map[string]any{
		"one":   filepath.Join(in, "one", "one.txt"),
		"many":  []any{filepath.Join(in, "many", "x.nc"), filepath.Join(in, "many", "y.nc")},
		"dir":   filepath.Join(in, "dir"),
		"count": int64(2),
	}
	if !reflect.DeepEqual(result.Inputs, want) {
		t.Fatalf("inputs = %#v, want %#v", result.Inputs, want)
	}
	if data, _ := os.ReadFile(filepath.Join(in, "dir", "sub", "b.json")); string(data) != "b" {
		t.Fatalf("bundle file holds %q", data)
	}
	if _, err := os.Stat(layout.InputsJSON()); err != nil {
		t.Fatal(err)
	}
}

func TestGroupBasenameCollision(t *testing.T) {
	src := t.TempDir()
	write(t, filepath.Join(src, "a", "x.nc"), "1")
	write(t, filepath.Join(src, "b", "x.nc"), "2")
	p := &params.Params{RawWith: map[string]any{
		"one":  filepath.Join(src, "a", "x.nc"),
		"many": []any{filepath.Join(src, "a", "x.nc"), filepath.Join(src, "b", "x.nc")},
	}}
	_, err := Run(context.Background(), Config{Tool: tool(t), Params: p, Layout: workdir.Layout{Root: t.TempDir()}, Resolver: prefix.NewResolver()})
	if err == nil || !strings.Contains(err.Error(), "basename") {
		t.Fatalf("error = %v, want a basename collision", err)
	}
}

// listing is a store whose bundle listing is controlled by the test.
type listing struct {
	prefix.Local
	paths []string
}

func (l listing) List(context.Context, string) ([]string, error) { return l.paths, nil }

type fixedResolver struct{ store prefix.Store }

func (r fixedResolver) For(context.Context, string) (prefix.Store, error) { return r.store, nil }

func TestBundleRefusesUnsafeNames(t *testing.T) {
	for _, bad := range []string{"../escape", "/abs", "a/../../b"} {
		err := downloadBundle(context.Background(), fixedResolver{listing{paths: []string{bad}}}, "s3://b/x", t.TempDir())
		if err == nil || !strings.Contains(err.Error(), "unsafe") {
			t.Errorf("%q: error = %v, want unsafe name", bad, err)
		}
	}
}

func TestFileNamesMustBeUsable(t *testing.T) {
	for _, uri := range []string{"s3://bucket/..", "/"} {
		if _, err := downloadFile(context.Background(), fixedResolver{prefix.Local{}}, uri, t.TempDir()); err == nil {
			t.Errorf("%q should be refused", uri)
		}
	}
}

func TestBindEvaluatesLetsInOrder(t *testing.T) {
	vars, err := Vars(map[string]any{"scene": map[string]any{"id": "s1"}}, map[string]params.Resolved{
		"count": {Outputs: map[string]any{"total": int64(2)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	lets := []params.Binding{
		{Name: "id", Value: "${{ scene.id }}"},
		{Name: "label", Value: "${{ id }}-of-${{ steps.count.outputs.total }}"},
		{Name: "fmt", Value: "COG"},
	}
	if err := Bind(vars, lets); err != nil {
		t.Fatal(err)
	}
	if vars["id"] != "s1" || vars["label"] != "s1-of-2" || vars["fmt"] != "COG" {
		t.Fatalf("vars = %#v", vars)
	}
	if err := Bind(vars, []params.Binding{{Name: "scene", Value: "x"}}); err == nil {
		t.Fatal("a let must not replace a scope variable")
	}
	err = Bind(vars, []params.Binding{{Name: "bad", Value: "${{ missing }}"}})
	if err == nil || !strings.Contains(err.Error(), "let.bad") {
		t.Fatalf("error = %v, want it to name let.bad", err)
	}
}

func TestIfSeesLets(t *testing.T) {
	p := &params.Params{
		Scope: map[string]any{"scene": map[string]any{"ok": false}},
		Let:   []params.Binding{{Name: "ok", Value: "${{ scene.ok }}"}},
		If:    "ok",
	}
	result, err := Run(context.Background(), Config{Tool: tool(t), Params: p, Layout: workdir.Layout{Root: t.TempDir()}})
	if err != nil || !result.Skipped {
		t.Fatalf("result = %+v, err = %v, want skipped", result, err)
	}
}

func TestIfMustBeBoolean(t *testing.T) {
	p := &params.Params{If: "${{ 'yes' }}"}
	_, err := Run(context.Background(), Config{Tool: tool(t), Params: p, Layout: workdir.Layout{Root: t.TempDir()}})
	if err == nil || !strings.Contains(err.Error(), "boolean") {
		t.Fatalf("error = %v", err)
	}
}

func TestIfAcceptsBareExpression(t *testing.T) {
	for condition, want := range map[string]bool{"1 > 2": false, "${{ 1 > 2 }}": false, "true && 2 > 1": true} {
		p := &params.Params{If: condition}
		result, err := Run(context.Background(), Config{Tool: tool(t), Params: p, Layout: workdir.Layout{Root: t.TempDir()}})
		if want {
			// The step runs on; stage then fails on the missing inputs, which is not under test.
			if result != nil && result.Skipped {
				t.Errorf("%q skipped the step", condition)
			}
			continue
		}
		if err != nil || !result.Skipped {
			t.Errorf("%q: result = %+v, err = %v, want skipped", condition, result, err)
		}
	}
}

func TestStagingNoneIsRefused(t *testing.T) {
	if err := CheckStaging("none"); err == nil {
		t.Fatal("staging none is not supported yet")
	}
	if err := CheckStaging("auto"); err != nil {
		t.Fatal(err)
	}
}
