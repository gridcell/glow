// Package cel evaluates the CEL expressions in `${{ }}` spans against the
// step environment. It declares the same custom functions as the Python
// evaluator so that both give the same results; the shared fixture in
// tests/fixtures/expressions/ checks this.
package cel

import (
	"fmt"
	"regexp"
	"sort"

	"cel.dev/cel-go/cel"
)

// costLimit bounds the work one expression may do. Glue expressions are
// small; the limit stops a hostile one from spinning on large inputs.
const costLimit = 1_000_000

var identifier = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// reserved names would shadow the function namespaces.
var reserved = map[string]bool{"path": true, "media": true}

// Evaluator evaluates expressions against a fixed set of variables.
type Evaluator struct {
	env  *cel.Env
	vars map[string]any
}

// New returns an Evaluator whose top-level variables are the keys of vars.
// Values must be JSON-like: maps with string keys, slices, strings, bools,
// int64, float64 and nil.
func New(vars map[string]any) (*Evaluator, error) {
	names := make([]string, 0, len(vars))
	for name := range vars {
		names = append(names, name)
	}
	sort.Strings(names)
	opts := []cel.EnvOption{library()}
	for _, name := range names {
		if !identifier.MatchString(name) || reserved[name] {
			return nil, fmt.Errorf("%q cannot be used as an expression variable", name)
		}
		opts = append(opts, cel.Variable(name, cel.DynType))
	}
	env, err := cel.NewEnv(opts...)
	if err != nil {
		return nil, err
	}
	return &Evaluator{env: env, vars: vars}, nil
}

// Eval compiles and evaluates one CEL expression and returns its value as
// JSON-like Go data.
func (e *Evaluator) Eval(expression string) (any, error) {
	ast, issues := e.env.Compile(expression)
	if issues != nil && issues.Err() != nil {
		return nil, issues.Err()
	}
	program, err := e.env.Program(ast, cel.CostLimit(costLimit))
	if err != nil {
		return nil, err
	}
	out, _, err := program.Eval(e.vars)
	if err != nil {
		return nil, err
	}
	return toJSON(out)
}
