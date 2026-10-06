package prefix

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
)

func TestLocalRoundTrip(t *testing.T) {
	ctx := context.Background()
	root := t.TempDir()
	src := filepath.Join(root, "src.txt")
	if err := os.WriteFile(src, []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}
	store := Local{}
	runPrefix := filepath.Join(root, "run")
	if err := store.Upload(ctx, src, Join(runPrefix, "out", "a.txt"), "text/plain"); err != nil {
		t.Fatal(err)
	}
	if err := store.Upload(ctx, src, Join("file://"+runPrefix, "out", "sub", "b.txt"), ""); err != nil {
		t.Fatal(err)
	}
	paths, err := store.List(ctx, Join(runPrefix, "out"))
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(paths)
	if want := []string{"a.txt", "sub/b.txt"}; !reflect.DeepEqual(paths, want) {
		t.Fatalf("List = %v, want %v", paths, want)
	}
	dest := filepath.Join(root, "in", "a.txt")
	if err := store.Download(ctx, Join(runPrefix, "out", "a.txt"), dest); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(dest); string(data) != "hello" {
		t.Fatalf("downloaded %q", data)
	}
}

func TestLocalRejectsRelativeAndDirectories(t *testing.T) {
	ctx := context.Background()
	if err := (Local{}).Download(ctx, "relative/a.txt", filepath.Join(t.TempDir(), "a")); err == nil {
		t.Fatal("relative paths should be rejected")
	}
	if err := (Local{}).Download(ctx, t.TempDir(), filepath.Join(t.TempDir(), "a")); err == nil {
		t.Fatal("directories should be rejected")
	}
}

func TestResolverSchemes(t *testing.T) {
	ctx := context.Background()
	r := NewResolver()
	for _, uri := range []string{"/tmp/x", "file:///tmp/x"} {
		if store, err := r.For(ctx, uri); err != nil || store != (Local{}) {
			t.Errorf("For(%q) = %v, %v; want Local", uri, store, err)
		}
	}
	for _, uri := range []string{"http://169.254.169.254/latest", "relative/path", "gs://bucket/x"} {
		if _, err := r.For(ctx, uri); err == nil {
			t.Errorf("For(%q) should fail", uri)
		}
	}
}

func TestJoin(t *testing.T) {
	if got := Join("s3://b/run/", "/out/", "a.tif"); got != "s3://b/run/out/a.tif" {
		t.Fatalf("Join = %q", got)
	}
}
