package cel

import (
	"reflect"
	"strings"
	"testing"
)

func TestFindExpressionsMatchesPythonRules(t *testing.T) {
	spans, err := FindExpressions("sst-${{ g.key }}-${{date(g.key, '%Y%m%d')}}.tif")
	if err != nil {
		t.Fatal(err)
	}
	if len(spans) != 2 || spans[0].Inner != "g.key" || spans[1].Inner != "date(g.key, '%Y%m%d')" {
		t.Fatalf("spans = %+v", spans)
	}
	if _, err := FindExpressions("${{ a "); err == nil {
		t.Fatal("unterminated expression should fail")
	}
}

func TestSubstituteWalksNestedValues(t *testing.T) {
	evaluator, err := New(map[string]any{"g": map[string]any{"key": "k1", "n": int64(2)}})
	if err != nil {
		t.Fatal(err)
	}
	got, err := evaluator.Substitute("with", map[string]any{
		"id":    "item-${{ g.key }}",
		"count": "${{ g.n + 1 }}",
		"size":  []any{int64(600), "${{ g.n }}"},
		"plain": true,
	})
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]any{
		"id":    "item-k1",
		"count": int64(3),
		"size":  []any{int64(600), int64(2)},
		"plain": true,
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v, want %#v", got, want)
	}
}

func TestSubstituteErrorNamesLocation(t *testing.T) {
	evaluator, err := New(map[string]any{"g": map[string]any{}})
	if err != nil {
		t.Fatal(err)
	}
	_, err = evaluator.Substitute("with", map[string]any{"assets": map[string]any{"href": "${{ g.missing }}"}})
	if err == nil || !strings.Contains(err.Error(), "with.assets.href") {
		t.Fatalf("error = %v, want location with.assets.href", err)
	}
}

func TestReservedVariableNames(t *testing.T) {
	if _, err := New(map[string]any{"path": "x"}); err == nil {
		t.Fatal("a variable named path should be rejected")
	}
	if _, err := New(map[string]any{"not-an-identifier": 1}); err == nil {
		t.Fatal("a non-identifier variable should be rejected")
	}
}

func TestCostLimit(t *testing.T) {
	evaluator, err := New(map[string]any{})
	if err != nil {
		t.Fatal(err)
	}
	expr := "[1,2,3,4,5,6,7,8,9,10].map(a, [1,2,3,4,5,6,7,8,9,10].map(b, [1,2,3,4,5,6,7,8,9,10].map(c, [1,2,3,4,5,6,7,8,9,10].map(d, [1,2,3,4,5,6,7,8,9,10].map(e, [1,2,3,4,5,6,7,8,9,10].map(f, a+b+c+d+e+f))))))"
	if _, err := evaluator.Eval(expr); err == nil || !strings.Contains(err.Error(), "cost") {
		t.Fatalf("error = %v, want a cost limit error", err)
	}
}

func TestDateEdgeCases(t *testing.T) {
	evaluator, err := New(map[string]any{})
	if err != nil {
		t.Fatal(err)
	}
	for _, expr := range []string{
		"date('2024', '%Y%')",
		"date('2024x', '%Y')",
		"date('2024', '%Q')",
		"date('2024-13-01', '%Y-%m-%d')",
	} {
		if _, err := evaluator.Eval(expr); err == nil {
			t.Errorf("%s should fail", expr)
		}
	}
	got, err := evaluator.Eval("date('2024-1-5', '%Y-%m-%d')")
	if err != nil || got != "2024-01-05T00:00:00Z" {
		t.Fatalf("single-digit fields: got %v, %v", got, err)
	}
}
