// Package manifest reads a toolpack manifest, or a single tool spec, and
// selects the tool a step runs. The Python models in src/glow/models are the
// source of truth for the format; this package reads only what the runtime
// needs and repeats the checks that protect the file system.
package manifest

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"path"
	"regexp"
	"strconv"
	"strings"

	"go.yaml.in/yaml/v3"

	"github.com/sparkgeo/glow/glow-exec/internal/jsonvalue"
)

// MaxSize bounds the manifest document, matching the 1 MiB limit of
// `glow validate`.
const MaxSize = 1 << 20

// Data kinds are the input and output types that refer to files.
const (
	KindFile   = "file"
	KindBundle = "bundle"
	KindGroup  = "group"
)

// identifier matches input and output names. They become directory names
// under /work/in/ and the run prefix, so nothing else is allowed.
var identifier = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,62}$`)

// Tool is the part of a tool spec that the runtime uses.
type Tool struct {
	Name     string                    `json:"name"`
	Inputs   map[string]map[string]any `json:"inputs"`
	Required []string                  `json:"required"`
	Outputs  map[string]Output         `json:"outputs"`
	Command  []string                  `json:"command"`
}

// Output is one declared output of a tool.
type Output struct {
	Type          string            `json:"type"`
	Path          string            `json:"path"`
	MediaType     string            `json:"media_type"`
	MediaTypeFrom string            `json:"media_type_from"`
	MediaTypes    map[string]string `json:"media_types"`
	Items         map[string]any    `json:"items"`
}

type toolpack struct {
	Toolpack string `json:"toolpack"`
	Version  int    `json:"version"`
	Tools    []Tool `json:"tools"`
}

// IsDataKind reports whether a value type refers to files.
func IsDataKind(valueType string) bool {
	return valueType == KindFile || valueType == KindBundle || valueType == KindGroup
}

// Load parses a manifest (YAML or JSON) and returns the tool named by ref,
// written `name@major`. The document is either a toolpack manifest with a
// `tools` list, whose `version` must equal the major, or a single tool spec,
// as the compiler passes for inline `run` and `script` steps.
func Load(data []byte, ref string) (*Tool, error) {
	if len(data) > MaxSize {
		return nil, fmt.Errorf("manifest is larger than %d bytes", MaxSize)
	}
	name, major, err := parseRef(ref)
	if err != nil {
		return nil, err
	}
	doc, err := toJSON(data)
	if err != nil {
		return nil, err
	}
	var probe map[string]json.RawMessage
	if err := json.Unmarshal(doc, &probe); err != nil {
		return nil, errors.New("manifest must be a mapping")
	}
	var tool *Tool
	if _, ok := probe["tools"]; ok {
		tool, err = selectTool(doc, name, major)
	} else {
		tool, err = singleTool(doc, name)
	}
	if err != nil {
		return nil, err
	}
	if err := tool.check(); err != nil {
		return nil, fmt.Errorf("tool %q: %w", tool.Name, err)
	}
	return tool, nil
}

func parseRef(ref string) (string, int, error) {
	name, majorText, found := strings.Cut(ref, "@")
	if !found || name == "" {
		return "", 0, fmt.Errorf("tool reference %q must be name@major", ref)
	}
	major, err := strconv.Atoi(majorText)
	if err != nil || major < 1 {
		return "", 0, fmt.Errorf("tool reference %q has an invalid major version", ref)
	}
	return name, major, nil
}

// toJSON converts YAML (a superset of JSON) to JSON so that one set of
// struct tags decodes both.
func toJSON(data []byte) ([]byte, error) {
	var doc any
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(&doc); err != nil {
		return nil, fmt.Errorf("manifest is not valid YAML: %w", err)
	}
	out, err := json.Marshal(doc)
	if err != nil {
		return nil, fmt.Errorf("manifest cannot be represented as JSON: %w", err)
	}
	return out, nil
}

func selectTool(doc []byte, name string, major int) (*Tool, error) {
	var pack toolpack
	if err := jsonvalue.Decode(doc, &pack); err != nil {
		return nil, fmt.Errorf("manifest: %w", err)
	}
	if pack.Version != major {
		return nil, fmt.Errorf("toolpack %q is version %d, not %d", pack.Toolpack, pack.Version, major)
	}
	for i := range pack.Tools {
		if pack.Tools[i].Name == name {
			return &pack.Tools[i], nil
		}
	}
	return nil, fmt.Errorf("toolpack %q has no tool %q", pack.Toolpack, name)
}

func singleTool(doc []byte, name string) (*Tool, error) {
	var tool Tool
	if err := jsonvalue.Decode(doc, &tool); err != nil {
		return nil, fmt.Errorf("manifest: %w", err)
	}
	if tool.Name != name {
		return nil, fmt.Errorf("manifest describes tool %q, not %q", tool.Name, name)
	}
	return &tool, nil
}

func (t *Tool) check() error {
	for _, decl := range t.Inputs {
		jsonvalue.Normalize(map[string]any(decl))
	}
	for name, decl := range t.Inputs {
		if !identifier.MatchString(name) {
			return fmt.Errorf("input name %q is not an identifier", name)
		}
		if _, ok := decl["type"].(string); !ok {
			return fmt.Errorf("input %q has no type", name)
		}
	}
	for _, name := range t.Required {
		if _, ok := t.Inputs[name]; !ok {
			return fmt.Errorf("required input %q is not declared", name)
		}
	}
	for name, out := range t.Outputs {
		if !identifier.MatchString(name) {
			return fmt.Errorf("output name %q is not an identifier", name)
		}
		if out.Path != "" && !IsLocalPath(out.Path) {
			return fmt.Errorf("output %q path %q must be relative to /work/out/ without '..'", name, out.Path)
		}
	}
	return nil
}

// IsLocalPath reports whether p is a relative slash path that stays inside
// the directory it is joined to.
func IsLocalPath(p string) bool {
	if p == "" || path.IsAbs(p) || strings.Contains(p, `\`) {
		return false
	}
	for _, part := range strings.Split(p, "/") {
		if part == ".." {
			return false
		}
	}
	return path.Clean(p) != "."
}

// InputType returns the declared type of an input, or "" if undeclared.
func (t *Tool) InputType(name string) string {
	valueType, _ := t.Inputs[name]["type"].(string)
	return valueType
}

// InputMediaTypes returns the media types an input accepts. An empty result
// means the manifest does not restrict them.
func (t *Tool) InputMediaTypes(name string) []string {
	switch value := t.Inputs[name]["media_type"].(type) {
	case string:
		return []string{value}
	case []any:
		types := make([]string, 0, len(value))
		for _, item := range value {
			if text, ok := item.(string); ok {
				types = append(types, text)
			}
		}
		return types
	default:
		return nil
	}
}

// Defaults returns the declared default of every input that has one.
func (t *Tool) Defaults() map[string]any {
	defaults := map[string]any{}
	for name, decl := range t.Inputs {
		if value, ok := decl["default"]; ok && value != nil {
			defaults[name] = value
		}
	}
	return defaults
}

// OutputMediaType returns the media type of a file output: the declared one,
// or the one chosen by the value of its media_type_from input. It returns ""
// when the manifest does not name one.
func (t *Tool) OutputMediaType(name string, inputs map[string]any) (string, error) {
	out := t.Outputs[name]
	if out.MediaTypeFrom == "" {
		return out.MediaType, nil
	}
	choice, ok := inputs[out.MediaTypeFrom]
	if !ok {
		return "", fmt.Errorf("output %q takes its media type from input %q, which is not set", name, out.MediaTypeFrom)
	}
	key := fmt.Sprint(choice)
	mediaType, ok := out.MediaTypes[key]
	if !ok {
		return "", fmt.Errorf("output %q has no media type for %s=%q", name, out.MediaTypeFrom, key)
	}
	return mediaType, nil
}
