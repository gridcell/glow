// Package workdir names the files of the tool contract under the work
// directory (/work in a pod). See docs/toolpacks.md.
package workdir

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// Default is the work directory inside a step pod.
const Default = "/work"

// Layout is a work directory.
type Layout struct {
	Root string
}

// InputsJSON is written by stage and read by the tool.
func (l Layout) InputsJSON() string { return filepath.Join(l.Root, "inputs.json") }

// In holds staged input files, one directory per input.
func (l Layout) In() string { return filepath.Join(l.Root, "in") }

// Out holds the tool's file outputs at their declared paths.
func (l Layout) Out() string { return filepath.Join(l.Root, "out") }

// OutputsJSON is written by the tool with its non-file outputs.
func (l Layout) OutputsJSON() string { return filepath.Join(l.Root, "outputs.json") }

// Resolved is written by collect, or by stage for a skipped step, and
// becomes the step's output parameter.
func (l Layout) Resolved() string { return filepath.Join(l.Root, "outputs.resolved.json") }

// Output is written by collect with the resolved value of one output, so
// that Argo can read each output as its own parameter. The name has been
// checked to be an identifier.
func (l Layout) Output(name string) string {
	return filepath.Join(l.Root, "outputs", name+".json")
}

// WriteJSON writes v as indented JSON to path through a temporary file, so
// that a reader never sees a partial document.
func WriteJSON(path string, v any) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
