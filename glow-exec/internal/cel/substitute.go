package cel

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

const (
	open  = "${{"
	close = "}}"
)

// Span is one `${{ ... }}` occurrence: value[Start:End] is the whole span
// and Inner the trimmed expression text. It mirrors find_expressions in
// src/glow/expressions/syntax.py: the first "}}" closes a span.
type Span struct {
	Start, End int
	Inner      string
}

// FindExpressions returns every `${{ ... }}` span in value, in order.
func FindExpressions(value string) ([]Span, error) {
	var spans []Span
	position := 0
	for {
		start := strings.Index(value[position:], open)
		if start == -1 {
			return spans, nil
		}
		start += position
		innerStart := start + len(open)
		closeAt := strings.Index(value[innerStart:], close)
		if closeAt == -1 {
			return nil, fmt.Errorf("unterminated expression starting at offset %d", start)
		}
		end := innerStart + closeAt + len(close)
		spans = append(spans, Span{Start: start, End: end, Inner: strings.TrimSpace(value[innerStart : innerStart+closeAt])})
		position = end
	}
}

// Template evaluates the expressions in one string. A string that is one
// span and nothing else keeps the expression's type; otherwise each result
// is rendered as text (strings as is, other values as JSON) and spliced in.
func (e *Evaluator) Template(value string) (any, error) {
	spans, err := FindExpressions(value)
	if err != nil || len(spans) == 0 {
		return value, err
	}
	if len(spans) == 1 && spans[0].Start == 0 && spans[0].End == len(value) {
		return e.evalSpan(spans[0])
	}
	var out strings.Builder
	last := 0
	for _, span := range spans {
		result, err := e.evalSpan(span)
		if err != nil {
			return nil, err
		}
		text, err := render(result)
		if err != nil {
			return nil, err
		}
		out.WriteString(value[last:span.Start])
		out.WriteString(text)
		last = span.End
	}
	out.WriteString(value[last:])
	return out.String(), nil
}

func (e *Evaluator) evalSpan(span Span) (any, error) {
	result, err := e.Eval(span.Inner)
	if err != nil {
		return nil, fmt.Errorf("evaluating %q: %w", span.Inner, err)
	}
	return result, nil
}

func render(value any) (string, error) {
	if text, ok := value.(string); ok {
		return text, nil
	}
	data, err := json.Marshal(value)
	return string(data), err
}

// Substitute evaluates every expression in a JSON-like value, walking maps
// and lists. Keys are not evaluated. location names the value in errors,
// for example "with".
func (e *Evaluator) Substitute(location string, value any) (any, error) {
	switch v := value.(type) {
	case string:
		result, err := e.Template(v)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", location, err)
		}
		return result, nil
	case map[string]any:
		out := make(map[string]any, len(v))
		keys := make([]string, 0, len(v))
		for key := range v {
			keys = append(keys, key)
		}
		// Sorted so that the first error reported is deterministic.
		sort.Strings(keys)
		for _, key := range keys {
			result, err := e.Substitute(location+"."+key, v[key])
			if err != nil {
				return nil, err
			}
			out[key] = result
		}
		return out, nil
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			result, err := e.Substitute(fmt.Sprintf("%s[%d]", location, i), item)
			if err != nil {
				return nil, err
			}
			out[i] = result
		}
		return out, nil
	default:
		return value, nil
	}
}
