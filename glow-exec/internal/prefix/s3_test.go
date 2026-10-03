//go:build s3

package prefix

// Run against MinIO or another S3-compatible service:
//
//	AWS_ENDPOINT_URL_S3=http://localhost:9000 GLOW_S3_PATH_STYLE=true \
//	AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_REGION=us-east-1 \
//	GLOW_TEST_S3_BUCKET=glow-test go test -tags s3 ./internal/prefix/

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestS3RoundTrip(t *testing.T) {
	bucket := os.Getenv("GLOW_TEST_S3_BUCKET")
	if bucket == "" {
		t.Skip("GLOW_TEST_S3_BUCKET is not set")
	}
	ctx := context.Background()
	store, err := NewS3(ctx)
	if err != nil {
		t.Fatal(err)
	}
	src := filepath.Join(t.TempDir(), "a.txt")
	if err := os.WriteFile(src, []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}
	runPrefix := "s3://" + bucket + "/glow-exec-test/" + filepath.Base(t.TempDir())
	if err := store.Upload(ctx, src, Join(runPrefix, "out", "a.txt"), "text/plain"); err != nil {
		t.Fatal(err)
	}
	paths, err := store.List(ctx, Join(runPrefix, "out"))
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"a.txt"}; !reflect.DeepEqual(paths, want) {
		t.Fatalf("List = %v, want %v", paths, want)
	}
	dest := filepath.Join(t.TempDir(), "a.txt")
	if err := store.Download(ctx, Join(runPrefix, "out", "a.txt"), dest); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(dest); string(data) != "hello" {
		t.Fatalf("downloaded %q", data)
	}
}
