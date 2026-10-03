package jsonvalue

import (
	"reflect"
	"testing"
)

func TestDecodeObjectNormalizesNumbers(t *testing.T) {
	got, err := DecodeObject([]byte(`{"i": 3, "f": 1.5, "big": 1e300, "list": [1, {"n": -2}]}`))
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"i": int64(3), "f": 1.5, "big": 1e300, "list": []any{int64(1), map[string]any{"n": int64(-2)}}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v, want %#v", got, want)
	}
}

func TestDecodeObjectRejects(t *testing.T) {
	for _, input := range []string{`[1]`, `"x"`, `{} {}`, `{`} {
		if _, err := DecodeObject([]byte(input)); err == nil {
			t.Errorf("DecodeObject(%s) should fail", input)
		}
	}
}
