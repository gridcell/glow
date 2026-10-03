import ast
from pathlib import Path

import pytest

import glow.types
from glow.builtins import BUILTINS
from glow.models import ToolInput, ToolOutput
from glow.types import (
    STRING,
    TIMESTAMP,
    Array,
    Bundle,
    Compatibility,
    File,
    GlowType,
    Group,
    MediaType,
    MediaTypeError,
    Scalar,
    Unknown,
    accepts,
    is_assignable,
    member_type,
    parse_media_types,
    parse_type,
    parse_type_or_unknown,
    render,
    resolve_output_type,
)

GEOTIFF = "image/tiff; application=geotiff"
COG = "image/tiff; application=geotiff; profile=cloud-optimized"
NETCDF = "application/x-netcdf"


def file_of(*media_types: str) -> File:
    return File(parse_media_types(list(media_types)))


# --- media types -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("image/png", "image/png"),
        ("IMAGE/PNG", "image/png"),
        (GEOTIFF, GEOTIFF),
        ("image/tiff;profile=cloud-optimized;application=geotiff", COG),
        ("  image/tiff ; Application = GeoTIFF  ", GEOTIFF),
        ('image/tiff; application="geotiff"', GEOTIFF),
        ('text/plain; note="a;b \\"c\\""', 'text/plain; note="a;b \\"c\\""'),
        ("application/vnd.gdal.vrt+xml", "application/vnd.gdal.vrt+xml"),
        ("*", "*"),
        ("*/*", "*"),
    ],
)
def test_parse_canonical(text: str, canonical: str) -> None:
    assert str(MediaType.parse(text)) == canonical


def test_parse_fields() -> None:
    media_type = MediaType.parse(COG)
    assert (media_type.type, media_type.subtype) == ("image", "tiff")
    assert media_type.params == (("application", "geotiff"), ("profile", "cloud-optimized"))
    assert not media_type.is_wildcard
    assert MediaType.parse("*").is_wildcard


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "type/subtype"),
        ("png", "type/subtype"),
        ("image/*", "type/subtype"),
        ("image/png; =x", "malformed parameter"),
        ("image/png; a", "malformed parameter"),
        ("image/png; a=b c", "malformed parameter"),
        ('image/png; a="unterminated', "malformed parameter"),
        ("image/tiff; a=1; A=2", "repeats parameter 'a'"),
        ("image/png" + "; a=b" * 60, "longer than 255"),
    ],
)
def test_parse_rejects(text: str, message: str) -> None:
    with pytest.raises(MediaTypeError, match=message):
        MediaType.parse(text)


def test_parse_media_types() -> None:
    assert parse_media_types(None) == ()
    assert parse_media_types("image/png") == (MediaType("image", "png"),)
    assert parse_media_types([GEOTIFF, "*"]) == (MediaType.parse(GEOTIFF), MediaType("*", "*"))


@pytest.mark.parametrize(
    ("accepted", "produced", "expected"),
    [
        (GEOTIFF, COG, True),
        (COG, GEOTIFF, False),
        (GEOTIFF, GEOTIFF, True),
        ("image/tiff", GEOTIFF, True),
        (GEOTIFF, "image/tiff", False),
        (GEOTIFF, "image/tiff; application=other", False),
        ("*", COG, True),
        ("*", "image/png", True),
        ("image/png", "image/tiff", False),
        ("image/png", GEOTIFF, False),
        (NETCDF, "APPLICATION/X-NETCDF", True),
        (NETCDF, "application/x-hdf5", False),
        ("image/png", "*", False),
    ],
)
def test_accepts(accepted: str, produced: str, expected: bool) -> None:
    assert accepts(accepted, produced) is expected
    assert accepts(MediaType.parse(accepted), MediaType.parse(produced)) is expected


# --- render ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("glow_type", "text"),
    [
        (file_of(GEOTIFF, "image/jp2"), "file[image/tiff; application=geotiff | image/jp2]"),
        (File(), "file"),
        (
            Bundle(parse_media_types("application/vnd.gdal.vrt+xml")),
            "bundle[application/vnd.gdal.vrt+xml]",
        ),
        (Group(), "group"),
        (Array(Scalar({"type": "object", "title": "stac-item"})), "array<stac-item>"),
        (Array(Array(Scalar({"type": "integer"}))), "array<array<integer>>"),
        (Scalar({}), "any"),
        (Unknown("why"), "unknown"),
        (file_of("*"), "file[*]"),
    ],
)
def test_render(glow_type: glow.types.GlowType, text: str) -> None:
    assert render(glow_type) == text


# --- data kinds --------------------------------------------------------------

TRANSLATE_SOURCE = file_of(GEOTIFF, "image/jp2", NETCDF, "application/vnd.gdal.vrt+xml")


@pytest.mark.parametrize(
    ("produced", "accepted", "status"),
    [
        (file_of(COG), file_of(GEOTIFF), "ok"),
        (file_of(GEOTIFF), file_of(COG), "mismatch"),
        (file_of("image/png"), file_of(GEOTIFF), "mismatch"),
        (file_of("image/png"), file_of("*"), "ok"),
        (file_of(NETCDF), TRANSLATE_SOURCE, "ok"),
        (Group(parse_media_types(NETCDF), captures=("date",)), TRANSLATE_SOURCE, "ok"),
        (Group(), TRANSLATE_SOURCE, "runtime_check"),
        (Group(), file_of("*"), "runtime_check"),
        (File(), file_of(GEOTIFF), "runtime_check"),
        (file_of("image/png"), File(), "ok"),
        (File(), File(), "ok"),
        (file_of(GEOTIFF, "image/png"), file_of(GEOTIFF), "runtime_check"),
        (file_of(COG, GEOTIFF), file_of(GEOTIFF), "ok"),
        (Bundle(parse_media_types("application/vnd.gdal.vrt+xml")), Bundle(), "ok"),
        (Bundle(), File(), "mismatch"),
        (File(), Bundle(), "mismatch"),
        (File(), Group(), "mismatch"),
        (Group(), Bundle(), "mismatch"),
    ],
)
def test_data_kinds(
    produced: glow.types.GlowType, accepted: glow.types.GlowType, status: str
) -> None:
    assert is_assignable(produced, accepted).status == status


def test_group_without_media_type_reason() -> None:
    result = is_assignable(Group(), file_of(GEOTIFF))
    assert result == Compatibility.runtime_check("the group has no declared media type")


def test_mismatch_names_both_types() -> None:
    result = is_assignable(file_of("image/png"), file_of(GEOTIFF))
    assert result.status == "mismatch"
    assert result.reason == (
        "expects: file[image/tiff; application=geotiff]\ngot:     file[image/png]"
    )
    assert result.hint is None


def test_array_of_files_into_file_is_a_mismatch() -> None:
    accepted = file_of(GEOTIFF, "image/jp2", NETCDF)
    result = is_assignable(Array(file_of(COG)), accepted)
    assert result.status == "mismatch"
    assert result.reason == (
        "expects: file[image/tiff; application=geotiff | image/jp2 | application/x-netcdf]\n"
        "got:     array<file[image/tiff; application=geotiff; profile=cloud-optimized]>"
    )
    assert result.hint == "for_each over the array"


def test_appendix_b_stac_items_into_file() -> None:
    items = Array(Scalar({"type": "object", "title": "stac-item"}))
    result = is_assignable(items, file_of(GEOTIFF))
    assert result.reason is not None
    assert result.reason.splitlines()[1] == "got:     array<stac-item>"
    assert result.hint == "for_each over the array"


@pytest.mark.parametrize(
    ("produced", "accepted", "status"),
    [
        (Array(file_of(COG)), Array(file_of(GEOTIFF)), "ok"),
        (Array(file_of("image/png")), Array(file_of(GEOTIFF)), "mismatch"),
        (Array(Group()), Array(file_of(GEOTIFF)), "runtime_check"),
        (file_of(COG), Array(file_of(GEOTIFF)), "mismatch"),
        (Array(Scalar({"type": "integer"})), Array(Scalar({"type": "number"})), "ok"),
        (Unknown("map built by an expression"), file_of(GEOTIFF), "runtime_check"),
        (file_of(GEOTIFF), Unknown("untyped input"), "runtime_check"),
        (Array(Unknown("items")), Array(File()), "runtime_check"),
    ],
)
def test_arrays_and_unknown(
    produced: glow.types.GlowType, accepted: glow.types.GlowType, status: str
) -> None:
    assert is_assignable(produced, accepted).status == status


def test_unknown_reason_is_kept() -> None:
    result = is_assignable(Unknown("map built by an expression"), File())
    assert result == Compatibility.runtime_check("map built by an expression")
    result = is_assignable(File(), Unknown("untyped input"))
    assert result == Compatibility.runtime_check("untyped input")


@pytest.mark.parametrize(
    ("produced", "accepted", "status"),
    [
        (Scalar({"type": "string", "format": "uri"}), file_of(GEOTIFF), "runtime_check"),
        (Scalar({"type": "integer"}), File(), "mismatch"),
        (Scalar({"type": "object"}), Group(), "mismatch"),
        (file_of(GEOTIFF), Scalar({"type": "string"}), "mismatch"),
    ],
)
def test_scalars_and_data_kinds(
    produced: glow.types.GlowType, accepted: glow.types.GlowType, status: str
) -> None:
    assert is_assignable(produced, accepted).status == status


# --- scalars -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("produced", "accepted", "status"),
    [
        ({"type": "string"}, {"type": "string"}, "ok"),
        ({"type": "integer"}, {"type": "number"}, "ok"),
        ({"type": "number"}, {"type": "integer"}, "mismatch"),
        ({"type": "string"}, {"type": "boolean"}, "mismatch"),
        ({}, {"type": "string"}, "runtime_check"),
        ({"type": "boolean"}, {}, "ok"),
        ({"type": "string", "enum": ["COG"]}, {"type": "string", "enum": ["COG", "PNG"]}, "ok"),
        (
            {"type": "string", "enum": ["COG", "JPEG"]},
            {"type": "string", "enum": ["COG"]},
            "mismatch",
        ),
        ({"type": "string"}, {"type": "string", "enum": ["COG"]}, "runtime_check"),
        ({"type": "string", "enum": ["COG"]}, {"type": "string"}, "ok"),
        ({"anyOf": [{"type": "string"}]}, {"type": "string"}, "runtime_check"),
        ({"type": "string"}, {"oneOf": [{"type": "string"}]}, "runtime_check"),
    ],
)
def test_scalars(produced: dict, accepted: dict, status: str) -> None:
    assert is_assignable(Scalar(produced), Scalar(accepted)).status == status


def test_enum_mismatch_names_extra_values() -> None:
    result = is_assignable(
        Scalar({"type": "string", "enum": ["COG", "JPEG"]}),
        Scalar({"type": "string", "enum": ["COG", "PNG"]}),
    )
    assert result.reason == "expects: string\ngot:     string\nvalues not accepted: 'JPEG'"


BBOX = {"type": "array", "items": {"type": "number"}}


@pytest.mark.parametrize(
    ("produced", "accepted", "status"),
    [
        ({"type": "object"}, {"type": "object"}, "ok"),
        (
            {"type": "object"},
            {"type": "object", "properties": {"a": {"type": "string"}}},
            "runtime_check",
        ),
        ({"type": "object"}, {"type": "object", "required": ["a"]}, "runtime_check"),
        (
            {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "string"}}},
            {"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]},
            "ok",
        ),
        (
            {"type": "object", "properties": {"b": {"type": "string"}}},
            {"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]},
            "mismatch",
        ),
        (
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"a": {"type": "number"}}},
            "mismatch",
        ),
        (
            {"type": "object", "properties": {"bbox": BBOX}},
            {"type": "object", "properties": {"bbox": BBOX}},
            "ok",
        ),
        (
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"a": {"type": "string", "enum": ["x"]}}},
            "runtime_check",
        ),
        (
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "additionalProperties": {"type": "string"}},
            "runtime_check",
        ),
        (
            {"type": "object", "properties": {}, "additionalProperties": True},
            {"type": "object"},
            "runtime_check",
        ),
        (
            {"type": "object", "properties": {}, "additionalProperties": False},
            {"type": "object", "additionalProperties": False},
            "ok",
        ),
        (
            {"type": "object", "enum": [{"a": 1}]},
            {"type": "object", "enum": [{"a": 2}]},
            "mismatch",
        ),
    ],
)
def test_objects(produced: dict, accepted: dict, status: str) -> None:
    assert is_assignable(Scalar(produced), Scalar(accepted)).status == status


def test_object_mismatch_details() -> None:
    missing = is_assignable(
        Scalar({"type": "object", "properties": {}}),
        Scalar({"type": "object", "required": ["href"]}),
    )
    assert missing.reason is not None
    assert missing.reason.endswith("missing required property 'href'")

    wrong = is_assignable(
        Scalar({"type": "object", "properties": {"a": {"type": "string"}}}),
        Scalar({"type": "object", "properties": {"a": {"type": "number"}}}),
    )
    assert wrong.reason is not None
    assert wrong.reason.endswith("\nproperty 'a'")

    nested = is_assignable(
        Scalar({"type": "object", "properties": {"f": {"type": "string", "enum": ["x", "y"]}}}),
        Scalar({"type": "object", "properties": {"f": {"type": "string", "enum": ["x"]}}}),
    )
    assert nested.reason is not None
    assert nested.reason.endswith("\nproperty 'f': values not accepted: 'y'")


def test_scalar_is_hashable() -> None:
    assert hash(Scalar({"type": "string", "enum": ["a"]})) == hash(
        Scalar({"enum": ["a"], "type": "string"})
    )
    assert len({Array(Scalar({"type": "integer"})), Array(Scalar({"type": "integer"}))}) == 1


# --- declarations ------------------------------------------------------------


def test_parse_type_from_declarations() -> None:
    assert parse_type({"type": "file", "media_type": GEOTIFF}) == file_of(GEOTIFF)
    assert parse_type({"type": "bundle"}) == Bundle()
    assert parse_type({"type": "group", "media_type": [NETCDF]}) == Group(parse_media_types(NETCDF))
    assert parse_type({"type": "array", "items": {"type": "file"}}) == Array(File())
    assert parse_type({"type": "array"}) == Array(Unknown("array items are not declared"))
    assert parse_type({"type": "string", "enum": ["a"]}) == Scalar(
        {"type": "string", "enum": ["a"]}
    )
    assert parse_type(ToolInput(type="file", media_type=["image/png"])) == file_of("image/png")
    assert parse_type(ToolOutput(type="integer")) == Scalar({"type": "integer"})


def test_parse_type_rejects_bad_media_type() -> None:
    with pytest.raises(MediaTypeError):
        parse_type({"type": "file", "media_type": "image/*"})


def test_parse_type_of_media_type_from_is_runtime() -> None:
    output = ToolOutput(type="file", media_type_from="format", media_types={"PNG": "image/png"})
    assert parse_type(output) == File(unknown_reason="the media type follows input 'format'")


@pytest.fixture
def translate_result() -> ToolOutput:
    return ToolOutput(
        type="file",
        path="out.{ext}",
        media_type_from="format",
        media_types={"COG": COG, "GTiff": GEOTIFF, "PNG": "image/png"},
    )


@pytest.mark.parametrize(
    ("with_values", "defaults", "expected"),
    [
        ({"format": "PNG"}, None, file_of("image/png")),
        ({"format": "GTiff"}, {"format": "COG"}, file_of(GEOTIFF)),
        ({}, {"format": "COG"}, file_of(COG)),
        (
            {"format": "${{ inputs.format }}"},
            None,
            File(unknown_reason="the media type follows input 'format', given as an expression"),
        ),
        ({}, None, File(unknown_reason="input 'format' is not set and has no default")),
        (
            {"format": "JPEG"},
            None,
            File(unknown_reason="no media type is declared for format='JPEG'"),
        ),
    ],
)
def test_resolve_output_type(
    translate_result: ToolOutput,
    with_values: dict,
    defaults: dict | None,
    expected: File,
) -> None:
    assert resolve_output_type(translate_result, with_values, defaults) == expected


def test_resolved_output_feeds_inputs(translate_result: ToolOutput) -> None:
    accepted = file_of(GEOTIFF)
    cog = resolve_output_type(translate_result, {"format": "COG"})
    png = resolve_output_type(translate_result, {"format": "PNG"})
    dynamic = resolve_output_type(translate_result, {"format": "${{ inputs.format }}"})
    assert is_assignable(cog, accepted).status == "ok"
    assert is_assignable(png, accepted).status == "mismatch"
    assert is_assignable(dynamic, accepted) == Compatibility.runtime_check(
        "the media type follows input 'format', given as an expression"
    )


def test_resolve_output_without_media_type_from() -> None:
    output = ToolOutput(type="file", media_type=NETCDF)
    assert resolve_output_type(output, {}) == file_of(NETCDF)


def test_builtin_group_files_need_a_runtime_check() -> None:
    groups = parse_type(BUILTINS["fs.group"].outputs["groups"])
    assert isinstance(groups, Array)
    assert isinstance(groups.member, Group)
    assert is_assignable(groups.member, TRANSLATE_SOURCE).status == "runtime_check"


def test_module_does_not_import_validator_or_cli() -> None:
    tree = ast.parse(Path(glow.types.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = ("glow.validation", "glow.cli", "glow.lint", "typer")
    assert not [name for name in imported if name.startswith(forbidden)]


PNG_TYPE = MediaType.parse("image/png")
STRING_TYPE = Scalar({"type": "string"})


@pytest.mark.parametrize(
    ("base", "path", "expected"),
    [
        (Array(STRING_TYPE), (0,), STRING_TYPE),
        (Array(STRING_TYPE), (None,), STRING_TYPE),
        (Group((PNG_TYPE,)), ("files", 0, "path"), File((PNG_TYPE,))),
        (Group((PNG_TYPE,)), ("key",), STRING_TYPE),
        (File((PNG_TYPE,)), ("uri",), File((PNG_TYPE,))),
        (Bundle((PNG_TYPE,)), ("media_type",), STRING_TYPE),
        (
            Scalar({"type": "object", "properties": {"n": {"type": "integer"}}}),
            ("n",),
            Scalar({"type": "integer"}),
        ),
        (
            Scalar({"type": "object", "additionalProperties": {"type": "file"}}),
            ("any",),
            File(),
        ),
        (Group(), ("captures", "date"), STRING_TYPE),
    ],
)
def test_member_type(base: GlowType, path: tuple[object, ...], expected: GlowType) -> None:
    assert member_type(base, path) == expected


@pytest.mark.parametrize(
    ("base", "path"),
    [
        (Array(STRING_TYPE), ("length",)),
        (Group(), ("bands",)),
        (File(), ("size",)),
        (Scalar({"type": "object"}), ("x",)),
        (Scalar({"type": "string"}), (0,)),
        (Unknown("why"), ("x", 0)),
    ],
)
def test_member_type_unknown(base: GlowType, path: tuple[object, ...]) -> None:
    assert isinstance(member_type(base, path), Unknown)


def test_member_type_ignores_a_non_schema_additional_properties() -> None:
    result = member_type(Scalar({"type": "object", "additionalProperties": True}), ["x"])
    assert result == Unknown("'x' is not a declared property")


def test_parse_type_or_unknown() -> None:
    assert parse_type_or_unknown({"type": "file", "media_type": None}) == File()
    assert parse_type_or_unknown(ToolInput.model_validate({"type": "string"})) == STRING
    result = parse_type_or_unknown({"type": "file", "media_type": "image/*"})
    assert isinstance(result, Unknown)
    assert "not a media type" in result.reason


def test_timestamp_renders_and_feeds_a_string() -> None:
    assert render(TIMESTAMP) == "timestamp"
    assert is_assignable(TIMESTAMP, STRING).status == "ok"
