// Package jsonvalue decodes JSON into the plain Go values the runtime
// passes around: maps with string keys, slices, strings, bools, nil, and
// numbers as int64 when integral and float64 otherwise. Keeping integers
// as int64 lets CEL see `3` as an int, as the Python evaluator does.
package jsonvalue

import (
	"bytes"
	"encoding/json"
	"errors"
)

// Decode decodes one JSON value into into, which must be a pointer to an
// `any`, a map or a struct. Trailing data is an error.
func Decode(data []byte, into any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := decoder.Decode(into); err != nil {
		return err
	}
	if decoder.More() {
		return errors.New("unexpected data after the JSON value")
	}
	return nil
}

// DecodeObject decodes a JSON object with normalized numbers.
func DecodeObject(data []byte) (map[string]any, error) {
	var value any
	if err := Decode(data, &value); err != nil {
		return nil, err
	}
	object, ok := Normalize(value).(map[string]any)
	if !ok {
		return nil, errors.New("must be a JSON object")
	}
	return object, nil
}

// Normalize replaces json.Number values in place with int64 or float64.
func Normalize(value any) any {
	switch v := value.(type) {
	case json.Number:
		if i, err := v.Int64(); err == nil {
			return i
		}
		f, _ := v.Float64()
		return f
	case map[string]any:
		for key, item := range v {
			v[key] = Normalize(item)
		}
		return v
	case []any:
		for i, item := range v {
			v[i] = Normalize(item)
		}
		return v
	default:
		return value
	}
}
