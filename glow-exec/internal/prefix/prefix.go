// Package prefix reads and writes run data under a URI: a local directory
// for development and tests, or an S3 prefix in a cluster.
package prefix

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// Store reads and writes the objects that URIs of one scheme address.
type Store interface {
	// Download copies the object at uri to the local file dest.
	Download(ctx context.Context, uri, dest string) error
	// Upload copies the local file src to uri.
	Upload(ctx context.Context, src, uri, mediaType string) error
	// List returns the slash-separated paths, relative to uri, of every
	// object under the prefix uri, recursively.
	List(ctx context.Context, uri string) ([]string, error)
}

// Resolver picks the Store for a URI. Upstream outputs may live under any
// run prefix, so the choice is made per URI, not once per step.
type Resolver struct {
	Local Store
	// S3 is created on first use so that local runs need no AWS setup.
	S3    Store
	newS3 func(context.Context) (Store, error)
}

// NewResolver returns a Resolver for local paths, file:// and s3:// URIs.
func NewResolver() *Resolver {
	return &Resolver{Local: Local{}, newS3: func(ctx context.Context) (Store, error) { return NewS3(ctx) }}
}

// For returns the Store for uri. Other schemes, such as http, are refused
// so that a workflow cannot make the runtime fetch arbitrary URLs.
func (r *Resolver) For(ctx context.Context, uri string) (Store, error) {
	switch {
	case strings.HasPrefix(uri, "s3://"):
		if r.S3 == nil {
			if r.newS3 == nil {
				return nil, fmt.Errorf("no S3 store configured for %q", uri)
			}
			store, err := r.newS3(ctx)
			if err != nil {
				return nil, fmt.Errorf("configuring S3: %w", err)
			}
			r.S3 = store
		}
		return r.S3, nil
	case strings.HasPrefix(uri, "file://"), strings.HasPrefix(uri, "/"):
		return r.Local, nil
	default:
		return nil, fmt.Errorf("unsupported URI %q: use an absolute path, file:// or s3://", uri)
	}
}

// Join appends slash-separated parts to a prefix with exactly one "/"
// between them.
func Join(prefix string, parts ...string) string {
	out := strings.TrimRight(prefix, "/")
	for _, part := range parts {
		out += "/" + strings.Trim(part, "/")
	}
	return out
}

// writeAtomic writes r to dest through a temporary file in the same
// directory, so a failed copy never leaves a partial file at dest.
func writeAtomic(dest string, r io.Reader) error {
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(dest), ".glow-*.part")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := io.Copy(tmp, r); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), dest)
}
