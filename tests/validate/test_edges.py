import pytest

from glow.models import ToolInput
from glow.types import Array, Bundle, File, GlowType, Group, MediaType, Scalar, Unknown
from glow.validate import Code
from glow.validate.edges import accepted_type, fs_group_type, member_type
from tests.validate.conftest import Check

PNG = MediaType.parse("image/png")
STRING = Scalar({"type": "string"})


@pytest.mark.parametrize(
    ("base", "path", "expected"),
    [
        (Array(STRING), (0,), STRING),
        (Array(STRING), (None,), STRING),
        (Group((PNG,)), ("files", 0, "path"), File((PNG,))),
        (Group((PNG,)), ("key",), STRING),
        (File((PNG,)), ("uri",), File((PNG,))),
        (Bundle((PNG,)), ("media_type",), STRING),
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
        (Group(), ("captures", "date"), STRING),
    ],
)
def test_member_type(base: GlowType, path: tuple[object, ...], expected: GlowType) -> None:
    assert member_type(base, path) == expected


@pytest.mark.parametrize(
    ("base", "path"),
    [
        (Array(STRING), ("length",)),
        (Group(), ("bands",)),
        (File(), ("size",)),
        (Scalar({"type": "object"}), ("x",)),
        (Scalar({"type": "string"}), (0,)),
        (Unknown("why"), ("x", 0)),
    ],
)
def test_member_type_unknown(base: GlowType, path: tuple[object, ...]) -> None:
    assert isinstance(member_type(base, path), Unknown)


def test_accepted_type_walks_nested_declarations() -> None:
    declaration = ToolInput.model_validate(
        {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {"href": {"type": "file", "media_type": "image/png"}},
            },
        }
    )
    assert accepted_type(declaration, ("data", "href")) == File((PNG,))
    assert isinstance(accepted_type(declaration, ("data", "title")), Unknown)
    sizes = ToolInput.model_validate({"type": "array", "items": {"type": "integer"}})
    assert accepted_type(sizes, (1,)) == Scalar({"type": "integer"})


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        (None, Group()),
        ("image/png", Group((PNG,))),
        ("${{ inputs.kind }}", Group(unknown_reason="the media type is not given as a constant")),
        (3, Group(unknown_reason="the media type is not given as a constant")),
    ],
)
def test_fs_group_type(media_type: object, expected: Group) -> None:
    assert fs_group_type(media_type) == expected


def test_fs_group_type_malformed() -> None:
    assert fs_group_type("image/*").unknown_reason is not None


HEADER = """\
name: t
inputs:
  scenes: { type: array }
steps:
"""


def test_block_using_its_own_outputs_is_one_cycle(check_yaml: Check) -> None:
    report = check_yaml(
        HEADER + "  - id: b\n    for_each: ${{ inputs.scenes }}\n    as: s\n    steps:\n"
        "      - id: m\n        uses: gdal.translate@1\n"
        "        with: { source: '${{ steps.b.outputs.r[0] }}' }\n"
        "    outputs: { r: '${{ steps.m.outputs.result }}' }\n"
    )
    assert [error.code for error in report.errors] == [Code.CYCLE]


def test_results_and_let_and_literal_list_types(check_yaml: Check) -> None:
    report = check_yaml(
        HEADER + "  - id: fan\n    for_each: [a, b]\n    as: name\n"
        "    let: { upper: '${{ name }}' }\n"
        "    run: echo\n    with: { v: '${{ upper }}' }\n"
        "    outputs: { n: { type: integer } }\n"
        "  - id: after\n    run: echo\n    with: { r: '${{ steps.fan.results[0] }}' }\n"
        "    outputs: { n: { type: integer } }\n"
    )
    assert report.ok, report.messages()
    assert report.ir is not None
    fan = report.ir.step("fan")
    assert fan.loop is not None and isinstance(fan.loop.over, Array)
    assert fan.outputs == {"n": Array(Scalar({"type": "integer"}))}
    [edge] = report.ir.edges_into("after")
    assert edge.source_step == "fan"
    assert isinstance(edge.type, Unknown)
