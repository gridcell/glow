package mediatype

import "testing"

func TestMatches(t *testing.T) {
	cases := []struct {
		actual, declared string
		want             bool
	}{
		{"image/png", "image/png", true},
		{"IMAGE/PNG", "image/png", true},
		{"image/png", "*", true},
		{"image/png", "image/*", true},
		{"text/plain", "image/*", false},
		{"image/tiff; application=geotiff; profile=cloud-optimized", "image/tiff; application=geotiff", true},
		{"image/tiff", "image/tiff; application=geotiff", false},
		{"image/tiff; application=geotiff", "image/tiff; application=geotiff; profile=cloud-optimized", false},
		{"image/jp2", "image/tiff", false},
	}
	for _, c := range cases {
		if got := Matches(c.actual, c.declared); got != c.want {
			t.Errorf("Matches(%q, %q) = %v, want %v", c.actual, c.declared, got, c.want)
		}
	}
}

func TestExt(t *testing.T) {
	if got := Ext("image/tiff; application=geotiff; profile=cloud-optimized"); got != "tif" {
		t.Errorf("Ext(COG) = %q, want tif", got)
	}
	if got := Ext("application/x-unknown"); got != "" {
		t.Errorf("Ext(unknown) = %q, want empty", got)
	}
}

func TestConsistentWithPath(t *testing.T) {
	if !ConsistentWithPath("out.TIFF", "image/tiff; application=geotiff") {
		t.Error("tiff file should be consistent with a GeoTIFF media type")
	}
	if ConsistentWithPath("out.png", "image/tiff") {
		t.Error("png file should not be consistent with image/tiff")
	}
	if !ConsistentWithPath("out.custom", "image/tiff") {
		t.Error("unknown extensions should be accepted")
	}
}
