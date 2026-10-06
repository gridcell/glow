package params

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

func TestLoadFromEnvironment(t *testing.T) {
	environ := []string{
		"GLOW_RAW_WITH=" + b64(`{"source": "${{ steps.a.outputs.result }}", "n": 3}`),
		"GLOW_MANIFEST=" + b64("name: x.y\n"),
		`GLOW_SCOPE={"inputs": {"k": 1.5}}`,
		"GLOW_LET=" + b64(`[{"name": "id", "value": "${{ scene.id }}"}, {"name": "fmt", "value": "COG"}]`),
		"GLOW_IF=${{ true }}",
		`GLOW_UPSTREAM_a={"outputs": {"result": {"uri": "/r/a/out.txt"}}, "skipped": false}`,
		`GLOW_UPSTREAM_b={"skipped": true}`,
		"GLOW_RUN_PREFIX=/runs/b",
	}
	p, err := Load(environ, Flags{})
	if err != nil {
		t.Fatal(err)
	}
	if p.RawWith["n"] != int64(3) || p.Scope["inputs"].(map[string]any)["k"] != 1.5 {
		t.Fatalf("numbers not normalized: %#v %#v", p.RawWith, p.Scope)
	}
	if string(p.Manifest) != "name: x.y\n" || p.If != "${{ true }}" || p.RunPrefix != "/runs/b" || p.WorkDir != "/work" {
		t.Fatalf("unexpected params %+v", p)
	}
	wantLet := []Binding{{Name: "id", Value: "${{ scene.id }}"}, {Name: "fmt", Value: "COG"}}
	if !reflect.DeepEqual(p.Let, wantLet) {
		t.Fatalf("let = %#v", p.Let)
	}
	if !p.Upstream["b"].Skipped || len(p.Upstream["b"].Outputs) != 0 {
		t.Fatalf("skipped upstream: %+v", p.Upstream["b"])
	}
	want := map[string]any{"result": map[string]any{"uri": "/r/a/out.txt"}}
	if !reflect.DeepEqual(p.Upstream["a"].Outputs, want) {
		t.Fatalf("upstream a = %#v", p.Upstream["a"].Outputs)
	}
}

func TestFlagsOverrideAndFiles(t *testing.T) {
	dir := t.TempDir()
	manifest := filepath.Join(dir, "m.yaml")
	if err := os.WriteFile(manifest, []byte("from-file"), 0o644); err != nil {
		t.Fatal(err)
	}
	upstreamFile := filepath.Join(dir, "a.json")
	if err := os.WriteFile(upstreamFile, []byte(`{"outputs": {"n": 1}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	p, err := Load([]string{"GLOW_MANIFEST=" + b64("from-env"), "GLOW_WORK_DIR=/env"}, Flags{
		Manifest: "@" + manifest,
		WorkDir:  "/flag",
		Upstream: []string{"a=@" + upstreamFile},
	})
	if err != nil {
		t.Fatal(err)
	}
	if string(p.Manifest) != "from-file" || p.WorkDir != "/flag" || p.Upstream["a"].Outputs["n"] != int64(1) {
		t.Fatalf("unexpected params %+v", p)
	}
}

func TestLoadRejectsMalformedInput(t *testing.T) {
	cases := map[string]Flags{
		"bad base64":       {RawWith: "%%%"},
		"with not object":  {RawWith: b64(`[1]`)},
		"trailing data":    {Scope: `{} {}`},
		"let not a list":   {Let: b64(`{"name": "a", "value": "x"}`)},
		"let bad name":     {Let: b64(`[{"name": "a.b", "value": "x"}]`)},
		"let bound twice":  {Let: b64(`[{"name": "a", "value": "x"}, {"name": "a", "value": "y"}]`)},
		"let not string":   {Let: b64(`[{"name": "a", "value": 1}]`)},
		"bad upstream id":  {Upstream: []string{"../x={}"}},
		"upstream no sep":  {Upstream: []string{"a"}},
		"skipped not bool": {Upstream: []string{`a={"skipped": "no"}`}},
		"outputs not map":  {Upstream: []string{`a={"outputs": []}`}},
	}
	for name, flags := range cases {
		if _, err := Load(nil, flags); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}
