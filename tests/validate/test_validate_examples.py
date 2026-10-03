from pathlib import Path

import pytest

from glow.validate import Code, validate
from tests.conftest import TOOLPACKS
from tests.validate.conftest import EXAMPLES, FIXTURES, Check


@pytest.mark.parametrize("name", ["sst-ingest.yaml", "minimal-if-script.yaml"])
def test_examples_are_valid(name: str) -> None:
    report = validate(EXAMPLES / name, TOOLPACKS)
    assert report.ok, report.messages()
    assert report.ir is not None


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("array-into-file.yaml", Code.TYPE_MISMATCH),
        ("unknown-tool.yaml", Code.UNKNOWN_TOOL),
        ("unknown-with-key.yaml", Code.UNKNOWN_WITH_KEY),
        ("missing-required.yaml", Code.MISSING_REQUIRED_INPUT),
        ("later-step.yaml", Code.LATER_STEP),
        ("block-member-outside.yaml", Code.BLOCK_MEMBER),
        ("undefined-loop-var.yaml", Code.UNDEFINED_NAME),
        ("cycle.yaml", Code.CYCLE),
        ("for-each-non-array.yaml", Code.FOR_EACH_NOT_ARRAY),
        ("undeclared-output.yaml", Code.UNDECLARED_OUTPUT),
        ("if-not-boolean.yaml", Code.IF_NOT_BOOLEAN),
    ],
)
def test_fixture_gives_exactly_one_error(fixture: str, code: Code) -> None:
    report = validate(FIXTURES / fixture, TOOLPACKS)
    assert not report.problems
    assert [error.code for error in report.errors] == [code], report.messages()
    assert report.ir is None


def test_codes_are_stable() -> None:
    assert {code.name: code.value for code in Code} == {
        "UNKNOWN_TOOL": "GLOW-E001",
        "UNKNOWN_WITH_KEY": "GLOW-E002",
        "MISSING_REQUIRED_INPUT": "GLOW-E003",
        "INVALID_MEDIA_TYPE": "GLOW-E004",
        "MALFORMED_EXPRESSION": "GLOW-E005",
        "UNDEFINED_NAME": "GLOW-E010",
        "LATER_STEP": "GLOW-E011",
        "BLOCK_MEMBER": "GLOW-E012",
        "UNDECLARED_OUTPUT": "GLOW-E013",
        "CYCLE": "GLOW-E020",
        "TYPE_MISMATCH": "GLOW-E030",
        "FOR_EACH_NOT_ARRAY": "GLOW-E031",
        "IF_NOT_BOOLEAN": "GLOW-E032",
        "REGISTRY_UNAVAILABLE": "GLOW-E040",
    }


def test_schema_problems_stop_before_semantic_checks() -> None:
    report = validate(FIXTURES.parent / "invalid-workflow.yaml", TOOLPACKS)
    assert report.problems
    assert not report.errors


def test_manifest_gets_schema_check_only() -> None:
    report = validate(TOOLPACKS / "gdal" / "manifest.yaml", TOOLPACKS)
    assert report.ok
    assert report.kind == "toolpack"
    assert report.ir is None


def test_missing_registry(tmp_path: Path) -> None:
    report = validate(EXAMPLES / "sst-ingest.yaml", tmp_path)
    assert [error.code for error in report.errors] == [Code.REGISTRY_UNAVAILABLE]


def test_builtins_need_no_registry(tmp_path: Path) -> None:
    path = tmp_path / "w.yaml"
    path.write_text(
        "name: t\ninputs:\n  root: { type: uri }\nsteps:\n"
        "  - id: tifs\n    uses: fs.glob\n"
        "    with: { root: '${{ inputs.root }}', pattern: '**/*.tif' }\n"
    )
    assert validate(path, tmp_path / "missing").ok


HEADER = """\
name: t
inputs:
  scene: { type: file, media_type: "image/tiff; application=geotiff" }
  scenes: { type: array }
  flag: { type: boolean }
steps:
"""


def codes(check_yaml: Check, steps: str) -> list[Code]:
    return [error.code for error in check_yaml(HEADER + steps).errors]


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        pytest.param(
            "  - id: a\n    uses: fs.group@1\n    with: { root: x, pattern: x, key: x }\n",
            [Code.UNKNOWN_TOOL],
            id="builtin-with-major",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.translate@7\n    with: { source: '${{ inputs.scene }}' }\n",
            [Code.UNKNOWN_TOOL],
            id="unknown-major",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.info@1\n    with: { source: '${{ inputs.scene' }\n",
            [Code.MALFORMED_EXPRESSION],
            id="unterminated-expression",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.info@1\n    with: { source: '${{ inputs.nope }}' }\n",
            [Code.UNDEFINED_NAME],
            id="undeclared-input",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.info@1\n"
            "    with: { source: '${{ steps.nope.outputs.x }}' }\n",
            [Code.UNDEFINED_NAME],
            id="undefined-step",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.info@1\n"
            "    with: { source: '${{ steps.a.outputs.info }}' }\n",
            [Code.CYCLE],
            id="self-reference",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.info@1\n    with: { source: '${{ inputs.scene }}' }\n"
            "  - id: b\n    uses: gdal.info@1\n    with: { source: '${{ steps.a.results }}' }\n",
            [Code.UNDECLARED_OUTPUT],
            id="results-on-plain-step",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.info@1\n    with: { source: '${{ inputs.scene }}' }\n"
            "  - id: b\n    uses: gdal.info@1\n    with: { source: '${{ steps.a.info }}' }\n",
            [Code.UNDECLARED_OUTPUT],
            id="missing-outputs-segment",
        ),
        pytest.param(
            "  - id: a\n    for_each: ${{ inputs.scenes }}\n    as: s\n"
            "    let: { first: '${{ steps.m.outputs.result }}' }\n    steps:\n"
            "      - id: m\n        uses: gdal.translate@1\n"
            "        with: { source: '${{ s.path }}' }\n"
            "    outputs: { r: '${{ steps.m.outputs.result }}' }\n",
            [Code.LATER_STEP],
            id="let-uses-member",
        ),
        pytest.param(
            "  - id: a\n    for_each: ${{ s }}\n    as: s\n"
            "    uses: gdal.info@1\n    with: { source: '${{ s }}' }\n",
            [Code.UNDEFINED_NAME],
            id="operand-cannot-see-loop-variable",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.info@1\n    with: { source: '${{ inputs.scene }}' }\n"
            "  - id: b\n    for_each: ${{ inputs.scenes }}\n    as: s\n    steps:\n"
            "      - id: c\n        uses: gdal.info@1\n"
            "        with: { source: '${{ steps.d.outputs.info }}' }\n"
            "    outputs: { r: '${{ steps.c.outputs.info }}' }\n"
            "  - id: d\n    uses: gdal.info@1\n    with: { source: '${{ inputs.scene }}' }\n",
            [Code.LATER_STEP],
            id="member-uses-later-outer-step",
        ),
        pytest.param(
            "  - id: a\n    run: echo\n"
            "    outputs: { out: { type: file, media_type: 'not a media type' } }\n",
            [Code.INVALID_MEDIA_TYPE],
            id="bad-output-media-type",
        ),
        pytest.param(
            "  - id: a\n    uses: fs.group\n"
            "    with: { root: x, pattern: x, key: x, media_type: 'image/*' }\n",
            [Code.INVALID_MEDIA_TYPE],
            id="bad-group-media-type",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.translate@1\n"
            "    with: { source: '${{ inputs.scene }}', format: '${{ inputs.flag }}' }\n",
            [Code.TYPE_MISMATCH],
            id="boolean-into-string",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.info@1\n"
            "    with: { source: '${{ inputs.scene }}', stats: 'x-${{ inputs.flag }}' }\n",
            [Code.TYPE_MISMATCH],
            id="interpolation-is-a-string",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.info@1\n    with:\n"
            "      source: ${{ inputs.scenes.map(s, s.path)[0] }}\n",
            [],
            id="macro-variable-is-local",
        ),
        pytest.param(
            "  - id: a\n    uses: gdal.info@1\n    if: ${{ inputs.flag }}\n"
            "    with: { source: '${{ inputs.scene }}' }\n",
            [],
            id="boolean-if",
        ),
    ],
)
def test_error_cases(check_yaml: Check, steps: str, expected: list[Code]) -> None:
    assert codes(check_yaml, steps) == expected


def test_unknown_tool_does_not_cascade(check_yaml: Check) -> None:
    steps = (
        "  - id: a\n    uses: gdal.nope@1\n    with: { anything: 1 }\n"
        "  - id: b\n    uses: gdal.info@1\n    with: { source: '${{ steps.a.outputs.x }}' }\n"
    )
    assert codes(check_yaml, steps) == [Code.UNKNOWN_TOOL]


def test_non_array_for_each_does_not_cascade_into_loop_variable(check_yaml: Check) -> None:
    steps = (
        "  - id: a\n    for_each: ${{ inputs.flag }}\n    as: s\n"
        "    uses: gdal.translate@1\n    with: { source: '${{ s }}' }\n"
    )
    assert codes(check_yaml, steps) == [Code.FOR_EACH_NOT_ARRAY]
