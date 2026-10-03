package cel

import (
	"github.com/sparkgeo/glow/glow-exec/internal/jsonvalue"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

type fixtureCase struct {
	Name       string `json:"name"`
	Env        string `json:"env"`
	Expression string `json:"expression"`
	Template   string `json:"template"`
	Result     any    `json:"result"`
	Error      string `json:"error"`
}

type fixture struct {
	Env   map[string]map[string]any `json:"env"`
	Cases []fixtureCase             `json:"cases"`
}

// TestSharedFixture runs the cases that the Python evaluator also runs.
func TestSharedFixture(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "tests", "fixtures", "expressions", "cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fx fixture
	if err := jsonvalue.Decode(data, &fx); err != nil {
		t.Fatal(err)
	}
	for _, c := range fx.Cases {
		t.Run(c.Name, func(t *testing.T) {
			env := jsonvalue.Normalize(map[string]any(fx.Env[c.Env])).(map[string]any)
			evaluator, err := New(env)
			if err != nil {
				t.Fatal(err)
			}
			var got any
			if c.Template != "" {
				got, err = evaluator.Template(c.Template)
			} else {
				got, err = evaluator.Eval(c.Expression)
			}
			if c.Error != "" {
				if err == nil || !strings.Contains(err.Error(), c.Error) {
					t.Fatalf("error = %v, want one containing %q", err, c.Error)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, jsonvalue.Normalize(c.Result)) {
				t.Fatalf("result = %#v, want %#v", got, c.Result)
			}
		})
	}
}
