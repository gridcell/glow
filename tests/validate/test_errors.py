from glow.validate import Code, GlowError, validate
from tests.conftest import TOOLPACKS
from tests.validate.conftest import FIXTURES


def test_array_into_file_matches_appendix_b() -> None:
    report = validate(FIXTURES / "array-into-file.yaml", TOOLPACKS)
    [error] = report.errors
    assert error.render().splitlines() == [
        "error: cog.with.source [GLOW-E030]",
        "  expects: file[image/tiff; application=geotiff | image/jp2 | application/x-netcdf"
        " | application/vnd.gdal.vrt+xml]",
        "  got:     array<group[application/x-netcdf]>  from ${{ steps.items.outputs.groups }}",
        "  hint:    for_each over the array, or .map(...) to extract one value per member",
    ]


def test_mismatch_detail_line_is_kept() -> None:
    error = GlowError(
        Code.TYPE_MISMATCH,
        "a.with.format",
        "values not accepted: 'JPEG'",
        expects="string",
        got="string",
        source="${{ inputs.format }}",
    )
    assert error.render().splitlines()[3] == "  values not accepted: 'JPEG'"


def test_render_neutralises_control_characters() -> None:
    error = GlowError(Code.UNKNOWN_WITH_KEY, "a.with.\x1b[2Jx", "line one\nline\x07two")
    rendered = error.render()
    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert rendered.splitlines()[0] == "error: a.with.?[2Jx [GLOW-E002]"


def test_render_clips_long_values() -> None:
    error = GlowError(Code.MALFORMED_EXPRESSION, "a.with.x", "y" * 1000)
    assert max(len(line) for line in error.render().splitlines()) < 210
