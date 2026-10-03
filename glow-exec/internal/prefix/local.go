package prefix

import (
	"context"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// Local is a Store over the local file system. URIs are absolute paths or
// file:// URIs.
type Local struct{}

func localPath(uri string) (string, error) {
	p := uri
	if rest, ok := strings.CutPrefix(uri, "file://"); ok {
		p = rest
	}
	if !filepath.IsAbs(p) {
		return "", fmt.Errorf("local URI %q is not an absolute path", uri)
	}
	return filepath.Clean(p), nil
}

// Download copies the regular file at uri to dest.
func (Local) Download(_ context.Context, uri, dest string) error {
	src, err := localPath(uri)
	if err != nil {
		return err
	}
	return copyRegular(src, dest)
}

// Upload copies the regular file src to uri.
func (Local) Upload(_ context.Context, src, uri, _ string) error {
	dest, err := localPath(uri)
	if err != nil {
		return err
	}
	return copyRegular(src, dest)
}

// List returns the regular files under uri. Symbolic links are skipped so a
// listing never leaves the directory.
func (Local) List(_ context.Context, uri string) ([]string, error) {
	root, err := localPath(uri)
	if err != nil {
		return nil, err
	}
	var paths []string
	err = filepath.WalkDir(root, func(p string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !entry.Type().IsRegular() {
			return nil
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		paths = append(paths, filepath.ToSlash(rel))
		return nil
	})
	if err != nil {
		return nil, err
	}
	return paths, nil
}

func copyRegular(src, dest string) error {
	info, err := os.Stat(src)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("%s is not a regular file", src)
	}
	f, err := os.Open(src)
	if err != nil {
		return err
	}
	defer f.Close()
	return writeAtomic(dest, f)
}
