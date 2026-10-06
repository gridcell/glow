package cel

import (
	"fmt"
	"math"
	"time"

	"cel.dev/cel-go/common/types"
	"cel.dev/cel-go/common/types/ref"
	"cel.dev/cel-go/common/types/traits"
)

// toJSON converts a CEL value to JSON-like Go data. Timestamps become
// RFC 3339 strings in UTC and durations become seconds strings such as
// "90s", which is how CEL's string() renders them.
func toJSON(val ref.Val) (any, error) {
	switch v := val.(type) {
	case types.Null:
		return nil, nil
	case types.Bool:
		return bool(v), nil
	case types.Int:
		return int64(v), nil
	case types.Uint:
		return uint64(v), nil
	case types.Double:
		f := float64(v)
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return nil, fmt.Errorf("result %v is not a JSON number", f)
		}
		return f, nil
	case types.String:
		return string(v), nil
	case types.Timestamp:
		return v.Time.UTC().Format(time.RFC3339Nano), nil
	case types.Duration:
		return string(v.ConvertToType(types.StringType).(types.String)), nil
	case traits.Mapper:
		return mapToJSON(v)
	case traits.Lister:
		return listToJSON(v)
	default:
		return nil, fmt.Errorf("result of type %s cannot be represented as JSON", val.Type().TypeName())
	}
}

func mapToJSON(m traits.Mapper) (any, error) {
	out := map[string]any{}
	for it := m.Iterator(); it.HasNext() == types.True; {
		key := it.Next()
		name, ok := key.(types.String)
		if !ok {
			return nil, fmt.Errorf("map key %v is not a string", key)
		}
		value, err := toJSON(m.Get(key))
		if err != nil {
			return nil, err
		}
		out[string(name)] = value
	}
	return out, nil
}

func listToJSON(l traits.Lister) (any, error) {
	out := []any{}
	for it := l.Iterator(); it.HasNext() == types.True; {
		value, err := toJSON(it.Next())
		if err != nil {
			return nil, err
		}
		out = append(out, value)
	}
	return out, nil
}
