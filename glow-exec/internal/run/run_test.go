package run

import (
	"bytes"
	"context"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestRunStreamsOutputInWorkDir(t *testing.T) {
	dir := t.TempDir()
	var stdout, stderr bytes.Buffer
	err := Run(context.Background(), Config{
		Command: []string{"/bin/sh", "-c", `pwd; echo "secret=${AWS_SECRET_ACCESS_KEY:-unset}"; echo oops >&2`},
		Dir:     dir,
		Env:     Env([]string{"PATH=/usr/bin:/bin", "AWS_SECRET_ACCESS_KEY=s3cr3t"}, dir),
		Stdout:  &stdout,
		Stderr:  &stderr,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(stdout.String(), dir) || !strings.Contains(stdout.String(), "secret=unset") {
		t.Fatalf("stdout = %q", stdout.String())
	}
	if stderr.String() != "oops\n" {
		t.Fatalf("stderr = %q", stderr.String())
	}
}

func TestRunReportsExitStatus(t *testing.T) {
	err := Run(context.Background(), Config{Command: []string{"/bin/sh", "-c", "exit 3"}, Dir: t.TempDir()})
	var exitErr *ExitError
	if !errors.As(err, &exitErr) || exitErr.Code != 3 {
		t.Fatalf("err = %v, want exit status 3", err)
	}
}

func TestRunMissingCommand(t *testing.T) {
	if err := Run(context.Background(), Config{Command: []string{"/no/such/tool"}, Dir: t.TempDir()}); err == nil {
		t.Fatal("expected an error")
	}
	if err := Run(context.Background(), Config{Dir: t.TempDir()}); err == nil {
		t.Fatal("expected an error for an empty command")
	}
}

func TestRunCancelStopsTool(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	start := time.Now()
	err := Run(ctx, Config{Command: []string{"/bin/sh", "-c", "sleep 30"}, Dir: t.TempDir()})
	if err == nil || time.Since(start) > 5*time.Second {
		t.Fatalf("err = %v after %s", err, time.Since(start))
	}
}

func TestEnvIsMinimal(t *testing.T) {
	env := Env([]string{"AWS_ACCESS_KEY_ID=x", "LANG=C.UTF-8"}, "/work")
	want := []string{"HOME=/work", "GLOW_WORK_DIR=/work", "PATH=" + defaultPath, "LANG=C.UTF-8"}
	if !slices.Equal(env, want) {
		t.Fatalf("Env = %v, want %v", env, want)
	}
}
