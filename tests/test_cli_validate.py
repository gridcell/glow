from pathlib import Path

import pytest
from typer.testing import CliRunner

from glow.cli import app
from glow.validation import MAX_DOCUMENT_BYTES

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

runner = CliRunner()


def validate(path: Path) -> tuple[int, str, str]:
    result = runner.invoke(app, ["validate", str(path)])
    return result.exit_code, result.stdout, result.stderr


@pytest.mark.parametrize(
    ("relative", "kind"),
    [
        ("examples/sst-ingest.yaml", "workflow"),
        ("examples/minimal-if-script.yaml", "workflow"),
        ("toolpacks/gdal/manifest.yaml", "toolpack"),
        ("toolpacks/stac/manifest.yaml", "toolpack"),
        ("toolpacks/prescient/manifest.yaml", "toolpack"),
    ],
)
def test_valid_files(relative: str, kind: str) -> None:
    code, stdout, stderr = validate(ROOT / relative)
    assert code == 0, stderr
    assert stdout.splitlines() == [f"ok: {ROOT / relative} is a valid {kind}"]


def test_invalid_workflow_reports_every_path() -> None:
    code, _, stderr = validate(FIXTURES / "invalid-workflow.yaml")
    assert code == 1
    lines = stderr.splitlines()
    assert "error: $.schedule: unknown property" in lines
    assert "error: $.steps[0].id: required property is missing" in lines
    assert (
        "error: $.steps[1]: step must set exactly one of uses, run, script, for_each "
        "(found: uses, run)"
    ) in lines
    assert lines[-1].endswith("3 problem(s) found")


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "doc.yaml"
    path.write_text(text)
    return path


def test_branch_error_prefers_matching_type(tmp_path: Path) -> None:
    path = write(tmp_path, "name: t\nsteps:\n  - id: a\n    uses: x.y\n    timeout: 5x\n")
    code, _, stderr = validate(path)
    assert code == 1
    assert "error: $.steps[0].timeout: '5x' does not match" in stderr


def test_non_mapping_document(tmp_path: Path) -> None:
    code, _, stderr = validate(write(tmp_path, "- a\n- b\n"))
    assert code == 1
    assert "error: $: expected object, got array" in stderr


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("name: t\nsteps: [\n", "invalid YAML"),
        ("a: &x [1]\nb: *x\n", "YAML aliases are not supported at line 2"),
        ("name: a\nname: b\n", "duplicate key 'name' at line 2"),
        ("name: t\non: push\n", "mapping key True must be a string"),
        ("x: !!python/object/apply:os.system [echo]\n", "invalid YAML"),
    ],
)
def test_unsafe_or_malformed_yaml(tmp_path: Path, text: str, message: str) -> None:
    code, _, stderr = validate(write(tmp_path, text))
    assert code == 1
    assert message in stderr


def test_missing_file(tmp_path: Path) -> None:
    code, _, stderr = validate(tmp_path / "missing.yaml")
    assert code == 2
    assert "cannot read" in stderr


def test_oversized_file(tmp_path: Path) -> None:
    code, _, stderr = validate(write(tmp_path, "#" * (MAX_DOCUMENT_BYTES + 1)))
    assert code == 2
    assert "larger than" in stderr
