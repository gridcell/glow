// Package mediatype compares media types and maps them to file extensions.
package mediatype

import (
	"path"
	"strings"
)

// Any is the wildcard media type that matches every value.
const Any = "*"

// extensions maps a base media type to its extensions. The first entry is
// the one used when a manifest path contains {ext}.
var extensions = map[string][]string{
	"image/tiff":                     {"tif", "tiff"},
	"image/png":                      {"png"},
	"image/jpeg":                     {"jpg", "jpeg"},
	"image/jp2":                      {"jp2"},
	"image/webp":                     {"webp"},
	"application/x-netcdf":           {"nc"},
	"application/x-hdf5":             {"h5", "hdf5"},
	"application/vnd.gdal.vrt+xml":   {"vrt"},
	"application/json":               {"json"},
	"application/geo+json":           {"geojson"},
	"application/vnd.apache.parquet": {"parquet"},
	"application/xml":                {"xml"},
	"application/zip":                {"zip"},
	"text/plain":                     {"txt"},
	"text/csv":                       {"csv"},
}

// byExtension is the reverse of extensions, built once.
var byExtension = func() map[string]string {
	index := map[string]string{}
	for base, exts := range extensions {
		for _, ext := range exts {
			index[ext] = base
		}
	}
	return index
}()

// Parsed is a media type split into its base type and parameters.
type Parsed struct {
	Base   string
	Params map[string]string
}

// Parse splits "type/subtype; key=value" into a lower-case base type and
// parameters. Parameter names are lower-cased; values keep their case.
func Parse(mediaType string) Parsed {
	parts := strings.Split(mediaType, ";")
	parsed := Parsed{Base: strings.ToLower(strings.TrimSpace(parts[0])), Params: map[string]string{}}
	for _, part := range parts[1:] {
		key, value, found := strings.Cut(part, "=")
		if !found {
			continue
		}
		parsed.Params[strings.ToLower(strings.TrimSpace(key))] = strings.Trim(strings.TrimSpace(value), `"`)
	}
	return parsed
}

// Matches reports whether a value of media type actual is acceptable where
// declared is expected. The base types must be equal, or declared is "*" or
// "type/*", and every parameter of declared must appear in actual with the
// same value. A more specific actual type therefore matches a more general
// declared one: a cloud-optimized GeoTIFF is a GeoTIFF.
func Matches(actual, declared string) bool {
	if strings.TrimSpace(declared) == Any {
		return true
	}
	want := Parse(declared)
	got := Parse(actual)
	if prefix, ok := strings.CutSuffix(want.Base, "/*"); ok {
		if !strings.HasPrefix(got.Base, prefix+"/") {
			return false
		}
	} else if got.Base != want.Base {
		return false
	}
	for key, value := range want.Params {
		if got.Params[key] != value {
			return false
		}
	}
	return true
}

// MatchesAny reports whether actual matches at least one declared type.
func MatchesAny(actual string, declared []string) bool {
	for _, candidate := range declared {
		if Matches(actual, candidate) {
			return true
		}
	}
	return false
}

// Ext returns the preferred extension for a media type, without a dot, or
// "" when the base type is unknown.
func Ext(mediaType string) string {
	exts := extensions[Parse(mediaType).Base]
	if len(exts) == 0 {
		return ""
	}
	return exts[0]
}

// BaseForPath returns the base media type for a file name's extension, or ""
// when the extension is unknown.
func BaseForPath(name string) string {
	ext := strings.ToLower(strings.TrimPrefix(path.Ext(name), "."))
	return byExtension[ext]
}

// ConsistentWithPath reports whether a file name's extension is consistent
// with a media type. Unknown extensions and the wildcard are accepted,
// because the table cannot list every format a tool may produce.
func ConsistentWithPath(name, mediaType string) bool {
	if strings.TrimSpace(mediaType) == Any {
		return true
	}
	base := BaseForPath(name)
	return base == "" || base == Parse(mediaType).Base
}
