package cel

import (
	"fmt"
	"strings"
	"time"

	"cel.dev/cel-go/cel"
	"cel.dev/cel-go/common/types"
	"cel.dev/cel-go/common/types/ref"

	"github.com/sparkgeo/glow/glow-exec/internal/mediatype"
)

// library declares the custom functions:
//
//	date(string, format) timestamp   parse with a strftime subset
//	path.basename(string) string      last path segment of a path or URI
//	path.stem(string) string          basename without its extension
//	path.ext(string) string           extension with its dot, or ""
//	path.join(string, string) string  join with exactly one "/"
//	media.matches(actual, declared) bool
//	media.ext(media_type) string      preferred extension, without a dot
func library() cel.EnvOption {
	str := cel.StringType
	return cel.Lib(funcLib{
		cel.Function("date", cel.Overload("date_string_string", []*cel.Type{str, str}, cel.TimestampType,
			cel.BinaryBinding(stringBinary(parseDate)))),
		cel.Function("path.basename", cel.Overload("path_basename_string", []*cel.Type{str}, str,
			cel.UnaryBinding(stringUnary(basename)))),
		cel.Function("path.stem", cel.Overload("path_stem_string", []*cel.Type{str}, str,
			cel.UnaryBinding(stringUnary(stem)))),
		cel.Function("path.ext", cel.Overload("path_ext_string", []*cel.Type{str}, str,
			cel.UnaryBinding(stringUnary(ext)))),
		cel.Function("path.join", cel.Overload("path_join_string_string", []*cel.Type{str, str}, str,
			cel.BinaryBinding(stringBinary(join)))),
		cel.Function("media.matches", cel.Overload("media_matches_string_string", []*cel.Type{str, str}, cel.BoolType,
			cel.BinaryBinding(stringBinary(func(actual, declared string) (ref.Val, error) {
				return types.Bool(mediatype.Matches(actual, declared)), nil
			})))),
		cel.Function("media.ext", cel.Overload("media_ext_string", []*cel.Type{str}, str,
			cel.UnaryBinding(stringUnary(mediaExt)))),
	})
}

type funcLib []cel.EnvOption

func (l funcLib) CompileOptions() []cel.EnvOption { return l }

func (funcLib) ProgramOptions() []cel.ProgramOption { return nil }

func stringUnary(fn func(string) (ref.Val, error)) func(ref.Val) ref.Val {
	return func(arg ref.Val) ref.Val {
		text, ok := arg.(types.String)
		if !ok {
			return types.MaybeNoSuchOverloadErr(arg)
		}
		return orErr(fn(string(text)))
	}
}

func stringBinary(fn func(string, string) (ref.Val, error)) func(ref.Val, ref.Val) ref.Val {
	return func(lhs, rhs ref.Val) ref.Val {
		left, ok := lhs.(types.String)
		if !ok {
			return types.MaybeNoSuchOverloadErr(lhs)
		}
		right, ok := rhs.(types.String)
		if !ok {
			return types.MaybeNoSuchOverloadErr(rhs)
		}
		return orErr(fn(string(left), string(right)))
	}
}

func orErr(val ref.Val, err error) ref.Val {
	if err != nil {
		return types.WrapErr(err)
	}
	return val
}

func basename(p string) (ref.Val, error) {
	return types.String(lastSegment(p)), nil
}

func lastSegment(p string) string {
	trimmed := strings.TrimRight(p, "/")
	return trimmed[strings.LastIndex(trimmed, "/")+1:]
}

// splitExt splits a basename like Python's os.path.splitext: leading dots
// do not start an extension.
func splitExt(name string) (string, string) {
	dot := strings.LastIndex(name, ".")
	if dot <= 0 || strings.Trim(name[:dot], ".") == "" {
		return name, ""
	}
	return name[:dot], name[dot:]
}

func stem(p string) (ref.Val, error) {
	base, _ := splitExt(lastSegment(p))
	return types.String(base), nil
}

func ext(p string) (ref.Val, error) {
	_, extension := splitExt(lastSegment(p))
	return types.String(extension), nil
}

func join(base, child string) (ref.Val, error) {
	if base == "" {
		return types.String(child), nil
	}
	return types.String(strings.TrimRight(base, "/") + "/" + strings.TrimLeft(child, "/")), nil
}

func mediaExt(mediaType string) (ref.Val, error) {
	extension := mediatype.Ext(mediaType)
	if extension == "" {
		return nil, fmt.Errorf("media.ext: no extension known for %q", mediaType)
	}
	return types.String(extension), nil
}

// dateField is one strftime directive: how many digits it reads at most
// and which component it sets.
type dateField struct {
	width int
	set   func(*dateParts, int)
}

type dateParts struct {
	year, month, day, hour, minute, second, yearDay int
}

var dateFields = map[byte]dateField{
	'Y': {4, func(p *dateParts, v int) { p.year = v }},
	'm': {2, func(p *dateParts, v int) { p.month = v }},
	'd': {2, func(p *dateParts, v int) { p.day = v }},
	'H': {2, func(p *dateParts, v int) { p.hour = v }},
	'M': {2, func(p *dateParts, v int) { p.minute = v }},
	'S': {2, func(p *dateParts, v int) { p.second = v }},
	'j': {3, func(p *dateParts, v int) { p.yearDay = v }},
}

// parseDate parses value with a strftime format limited to %Y %m %d %H %M
// %S %j and %%. Like Python's strptime, each directive reads one digit up
// to its width. The result is a UTC timestamp.
func parseDate(value, format string) (ref.Val, error) {
	parts := dateParts{year: 1900, month: 1, day: 1}
	pos := 0
	for i := 0; i < len(format); i++ {
		c := format[i]
		if c == '%' {
			if i+1 == len(format) {
				return nil, fmt.Errorf("date: format %q ends with %%", format)
			}
			i++
			c = format[i]
			if c != '%' {
				used, err := parts.read(c, value[pos:])
				if err != nil {
					return nil, err
				}
				if used == 0 {
					return nil, fmt.Errorf("date: %q does not match format %q", value, format)
				}
				pos += used
				continue
			}
		}
		if pos >= len(value) || value[pos] != c {
			return nil, fmt.Errorf("date: %q does not match format %q", value, format)
		}
		pos++
	}
	if pos != len(value) {
		return nil, fmt.Errorf("date: %q has text after format %q", value, format)
	}
	return parts.timestamp(value)
}

// read applies one directive to the start of text and returns the digits used.
func (p *dateParts) read(directive byte, text string) (int, error) {
	field, ok := dateFields[directive]
	if !ok {
		return 0, fmt.Errorf("date: unsupported directive %%%c", directive)
	}
	number, used := readDigits(text, field.width)
	field.set(p, number)
	return used, nil
}

func readDigits(text string, width int) (int, int) {
	number, used := 0, 0
	for used < width && used < len(text) && text[used] >= '0' && text[used] <= '9' {
		number = number*10 + int(text[used]-'0')
		used++
	}
	return number, used
}

func (p dateParts) timestamp(value string) (ref.Val, error) {
	t := time.Date(p.year, time.Month(p.month), p.day, p.hour, p.minute, p.second, 0, time.UTC)
	if p.yearDay > 0 {
		t = time.Date(p.year, time.January, p.yearDay, p.hour, p.minute, p.second, 0, time.UTC)
		if t.Year() != p.year {
			return nil, fmt.Errorf("date: %q is not a valid date", value)
		}
		return types.Timestamp{Time: t}, nil
	}
	// time.Date normalizes out-of-range fields (Feb 30 becomes Mar 2); a
	// round trip that changes a field means the input was not a real date.
	if t.Month() != time.Month(p.month) || t.Day() != p.day || t.Hour() != p.hour ||
		t.Minute() != p.minute || t.Second() != p.second {
		return nil, fmt.Errorf("date: %q is not a valid date", value)
	}
	return types.Timestamp{Time: t}, nil
}
