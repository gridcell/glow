package manifest

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

// fileRef is the JSON Schema of a value that refers to one file or bundle:
// a URI or path, or a resolved upstream output `{uri, media_type, kind}`.
func fileRef() map[string]any {
	return map[string]any{"anyOf": []any{
		map[string]any{"type": "string", "minLength": 1},
		map[string]any{
			"type":     "object",
			"required": []any{"uri"},
			"properties": map[string]any{
				"uri":        map[string]any{"type": "string", "minLength": 1},
				"media_type": map[string]any{"type": "string"},
				"kind":       map[string]any{"type": "string"},
			},
		},
	}}
}

// groupRef is the JSON Schema of a group value: a list of files, or an
// object with a `files` list such as a resolved group output or an
// `fs.group` entry.
func groupRef() map[string]any {
	files := map[string]any{"type": "array", "items": fileRef()}
	return map[string]any{"anyOf": []any{
		files,
		map[string]any{"type": "object", "required": []any{"files"}, "properties": map[string]any{"files": files}},
	}}
}

// toSchema converts a manifest input or output declaration to JSON Schema.
// Data kinds become file or group references and the manifest-only
// annotations are dropped.
func toSchema(decl map[string]any) map[string]any {
	switch decl["type"] {
	case KindFile, KindBundle:
		return fileRef()
	case KindGroup:
		return groupRef()
	}
	schema := map[string]any{}
	for key, value := range decl {
		switch key {
		case "media_type", "remote":
		case "items", "additionalProperties":
			if nested, ok := value.(map[string]any); ok {
				value = toSchema(nested)
			}
			schema[key] = value
		case "properties":
			props := map[string]any{}
			if nested, ok := value.(map[string]any); ok {
				for name, prop := range nested {
					if propDecl, ok := prop.(map[string]any); ok {
						props[name] = toSchema(propDecl)
					}
				}
			}
			schema[key] = props
		default:
			schema[key] = value
		}
	}
	return schema
}

func compile(id string, schema map[string]any) (*jsonschema.Schema, error) {
	compiler := jsonschema.NewCompiler()
	compiler.DefaultDraft(jsonschema.Draft2020)
	url := "mem://glow-exec/" + id + ".json"
	if err := compiler.AddResource(url, schema); err != nil {
		return nil, err
	}
	return compiler.Compile(url)
}

// ValidateInput checks one resolved input value against its declaration.
func (t *Tool) ValidateInput(name string, value any) error {
	decl, ok := t.Inputs[name]
	if !ok {
		return fmt.Errorf("input %q is not declared by tool %q", name, t.Name)
	}
	return validate("input/"+name, toSchema(decl), value)
}

// ValidateOutput checks one non-file output value against its declaration.
func (t *Tool) ValidateOutput(name string, value any) error {
	out := t.Outputs[name]
	decl := map[string]any{"type": out.Type}
	if out.Items != nil {
		decl["items"] = out.Items
	}
	return validate("output/"+name, toSchema(decl), value)
}

func validate(id string, schema map[string]any, value any) error {
	compiled, err := compile(id, schema)
	if err != nil {
		return fmt.Errorf("invalid declaration: %w", err)
	}
	err = compiled.Validate(value)
	var verr *jsonschema.ValidationError
	if errors.As(err, &verr) {
		return errors.New(describe(verr))
	}
	return err
}

// describe flattens a validation error to one line per failing location,
// without the schema URLs the library includes by default.
func describe(verr *jsonschema.ValidationError) string {
	var lines []string
	var walk func(unit jsonschema.OutputUnit)
	walk = func(unit jsonschema.OutputUnit) {
		if unit.Error != nil && len(unit.Errors) == 0 {
			location := unit.InstanceLocation
			if location == "" {
				location = "value"
			}
			lines = append(lines, location+": "+unit.Error.String())
		}
		for _, child := range unit.Errors {
			walk(child)
		}
	}
	walk(*verr.BasicOutput())
	if len(lines) == 0 {
		return verr.Error()
	}
	sort.Strings(lines)
	return strings.Join(lines, "; ")
}
