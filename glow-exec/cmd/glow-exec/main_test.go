package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// step is one glow-exec invocation in a test.
type step struct {
	tool     string
	with     string
	ifExpr   string
	upstream map[string]string // step id -> outputs.resolved.json content
	command  []string
}

type outcome struct {
	code    int
	stderr  string
	workDir string
	prefix  string
}

func testdata(t *testing.T, name string) string {
	t.Helper()
	path, err := filepath.Abs(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return path
}

func invoke(t *testing.T, s step) outcome {
	t.Helper()
	root := t.TempDir()
	o := outcome{workDir: filepath.Join(root, "work"), prefix: filepath.Join(root, "runs", "step")}
	if err := os.MkdirAll(o.workDir, 0o755); err != nil {
		t.Fatal(err)
	}
	environ := []string{
		"PATH=/usr/bin:/bin",
		"GLOW_RUN_PREFIX=" + o.prefix,
		"GLOW_RAW_WITH=" + base64.StdEncoding.EncodeToString([]byte(s.with)),
		"GLOW_SCOPE={}",
		"AWS_SECRET_ACCESS_KEY=must-not-leak",
	}
	if s.ifExpr != "" {
		environ = append(environ, "GLOW_IF="+s.ifExpr)
	}
	for id, resolved := range s.upstream {
		environ = append(environ, "GLOW_UPSTREAM_"+id+"="+resolved)
	}
	args := []string{"run", "--tool", s.tool, "--manifest", "@" + testdata(t, "manifest.yaml"), "--work-dir", o.workDir}
	args = append(args, "--")
	args = append(args, s.command...)
	var stdout, stderr bytes.Buffer
	o.code = Main(context.Background(), args, environ, &stdout, &stderr)
	o.stderr = stderr.String()
	return o
}

func readJSON(t *testing.T, path string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var value map[string]any
	if err := json.Unmarshal(data, &value); err != nil {
		t.Fatal(err)
	}
	return value
}

func toolRan(o outcome) bool {
	_, err := os.Stat(filepath.Join(o.workDir, "ran.marker"))
	return err == nil
}

func writeStep(t *testing.T) step {
	return step{tool: "fake.write@1", with: `{"message": "hi"}`, command: []string{"/bin/sh", testdata(t, "write.sh")}}
}

func copyStep(t *testing.T, with, upstreamA string) step {
	return step{
		tool:     "fake.copy@1",
		with:     with,
		upstream: map[string]string{"a": upstreamA},
		command:  []string{"/bin/sh", testdata(t, "copy.sh")},
	}
}

func TestChainedSteps(t *testing.T) {
	a := invoke(t, writeStep(t))
	if a.code != 0 {
		t.Fatalf("step a exited %d: %s", a.code, a.stderr)
	}
	resolvedA, err := os.ReadFile(filepath.Join(a.workDir, "outputs.resolved.json"))
	if err != nil {
		t.Fatal(err)
	}
	outputsA := readJSON(t, filepath.Join(a.workDir, "outputs.resolved.json"))["outputs"].(map[string]any)
	result := outputsA["result"].(map[string]any)
	if result["uri"] != filepath.Join(a.prefix, "result", "out.txt") || result["media_type"] != "text/plain" || result["kind"] != "file" {
		t.Fatalf("step a result = %v", result)
	}
	if outputsA["lines"] != float64(1) {
		t.Fatalf("step a lines = %v", outputsA["lines"])
	}

	b := invoke(t, copyStep(t, `{"source": "${{ steps.a.outputs.result }}", "label": "x-${{ steps.a.outputs.lines }}"}`, string(resolvedA)))
	if b.code != 0 {
		t.Fatalf("step b exited %d: %s", b.code, b.stderr)
	}
	inputsB := readJSON(t, filepath.Join(b.workDir, "inputs.json"))
	if inputsB["source"] != filepath.Join(b.workDir, "in", "source", "out.txt") || inputsB["label"] != "x-1" {
		t.Fatalf("step b inputs = %v", inputsB)
	}
	resolvedB := readJSON(t, filepath.Join(b.workDir, "outputs.resolved.json"))
	if resolvedB["skipped"] != false {
		t.Fatalf("step b skipped = %v", resolvedB["skipped"])
	}
	copied := resolvedB["outputs"].(map[string]any)["copy"].(map[string]any)
	uri := copied["uri"].(string)
	if !strings.HasPrefix(uri, b.prefix+"/") {
		t.Fatalf("step b uri %q is not under %q", uri, b.prefix)
	}
	if data, _ := os.ReadFile(uri); string(data) != "hello\n" {
		t.Fatalf("uploaded copy holds %q", data)
	}
	if strings.Contains(b.stderr, "must-not-leak") {
		t.Fatal("credentials leaked to the tool")
	}
}

func TestPathAliasForFileOutputs(t *testing.T) {
	a := invoke(t, writeStep(t))
	resolvedA, _ := os.ReadFile(filepath.Join(a.workDir, "outputs.resolved.json"))
	b := invoke(t, copyStep(t, `{"source": "${{ steps.a.outputs.result.path }}"}`, string(resolvedA)))
	if b.code != 0 {
		t.Fatalf("step b exited %d: %s", b.code, b.stderr)
	}
}

func TestSchemaViolationFailsBeforeTheToolRuns(t *testing.T) {
	a := invoke(t, writeStep(t))
	resolvedA, _ := os.ReadFile(filepath.Join(a.workDir, "outputs.resolved.json"))
	b := invoke(t, copyStep(t, `{"source": "${{ steps.a.outputs.result }}", "label": 42}`, string(resolvedA)))
	if b.code != exitError {
		t.Fatalf("exit = %d, want %d", b.code, exitError)
	}
	if !strings.Contains(b.stderr, `input "label" value 42`) {
		t.Fatalf("stderr does not name the input and value: %s", b.stderr)
	}
	if toolRan(b) {
		t.Fatal("the tool ran despite invalid inputs")
	}
}

func TestInputMediaTypeMismatch(t *testing.T) {
	upstream := `{"outputs": {"result": {"uri": "/nowhere/out.png", "media_type": "image/png", "kind": "file"}}}`
	b := invoke(t, copyStep(t, `{"source": "${{ steps.a.outputs.result }}"}`, upstream))
	if b.code != exitError || !strings.Contains(b.stderr, `input "source"`) || !strings.Contains(b.stderr, "image/png") {
		t.Fatalf("exit %d, stderr: %s", b.code, b.stderr)
	}
	if toolRan(b) {
		t.Fatal("the tool ran despite a media type mismatch")
	}
}

func TestMissingOutputNamesOutputAndPath(t *testing.T) {
	s := writeStep(t)
	s.command = []string{"/bin/sh", "-c", `printf '{"lines": 1}' > outputs.json`}
	o := invoke(t, s)
	if o.code != exitError {
		t.Fatalf("exit = %d, want %d", o.code, exitError)
	}
	want := filepath.Join(o.workDir, "out", "out.txt")
	if !strings.Contains(o.stderr, `output "result"`) || !strings.Contains(o.stderr, want) {
		t.Fatalf("stderr does not name the output and %s: %s", want, o.stderr)
	}
}

func TestMissingValueOutput(t *testing.T) {
	s := writeStep(t)
	s.command = []string{"/bin/sh", "-c", `printf x > out/out.txt`}
	o := invoke(t, s)
	if o.code != exitError || !strings.Contains(o.stderr, `output "lines"`) {
		t.Fatalf("exit %d, stderr: %s", o.code, o.stderr)
	}
}

func TestIfFalseSkipsTheTool(t *testing.T) {
	s := writeStep(t)
	s.ifExpr = "${{ 1 > 2 }}"
	o := invoke(t, s)
	if o.code != 0 {
		t.Fatalf("exit = %d: %s", o.code, o.stderr)
	}
	resolved := readJSON(t, filepath.Join(o.workDir, "outputs.resolved.json"))
	if len(resolved) != 1 || resolved["skipped"] != true {
		t.Fatalf("resolved = %v, want the skipped marker", resolved)
	}
	if toolRan(o) {
		t.Fatal("the tool ran although if was false")
	}
}

func TestSkippedUpstreamHasNoOutputs(t *testing.T) {
	b := invoke(t, step{
		tool:     "fake.write@1",
		with:     `{"message": "hi"}`,
		ifExpr:   "${{ size(steps.a.outputs) > 0 }}",
		upstream: map[string]string{"a": `{"skipped": true}`},
		command:  []string{"/bin/sh", testdata(t, "write.sh")},
	})
	if b.code != 0 || toolRan(b) {
		t.Fatalf("exit %d, ran %v: %s", b.code, toolRan(b), b.stderr)
	}
}

func TestToolExitStatusPassesThrough(t *testing.T) {
	s := writeStep(t)
	s.command = []string{"/bin/sh", "-c", "exit 7"}
	if o := invoke(t, s); o.code != 7 {
		t.Fatalf("exit = %d, want 7", o.code)
	}
}

func TestOutputPathOutsideOutIsRefused(t *testing.T) {
	a := invoke(t, writeStep(t))
	resolvedA, _ := os.ReadFile(filepath.Join(a.workDir, "outputs.resolved.json"))
	s := copyStep(t, `{"source": "${{ steps.a.outputs.result }}"}`, string(resolvedA))
	s.command = []string{"/bin/sh", "-c", `printf '{"copy": "../inputs.json"}' > outputs.json`}
	if o := invoke(t, s); o.code != exitError || !strings.Contains(o.stderr, "must stay under") {
		t.Fatalf("exit %d, stderr: %s", o.code, o.stderr)
	}
	s.command = []string{"/bin/sh", "-c", `ln -s ../inputs.json out/copy.txt && printf '{"copy": "copy.txt"}' > outputs.json`}
	if o := invoke(t, s); o.code != exitError || !strings.Contains(o.stderr, "points outside") {
		t.Fatalf("exit %d, stderr: %s", o.code, o.stderr)
	}
}

func TestUsageErrors(t *testing.T) {
	var stderr bytes.Buffer
	for _, args := range [][]string{nil, {"bogus"}, {"run"}, {"eval"}} {
		if code := Main(context.Background(), args, nil, &bytes.Buffer{}, &stderr); code != exitUsage {
			t.Errorf("Main(%v) = %d, want %d", args, code, exitUsage)
		}
	}
}

func TestEvalCommand(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := Main(context.Background(), []string{"eval", "--env", `{"g": {"key": "20240105"}}`, "date(g.key, '%Y%m%d')"}, nil, &stdout, &stderr)
	if code != 0 || strings.TrimSpace(stdout.String()) != `"2024-01-05T00:00:00Z"` {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}
