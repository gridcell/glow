from pathlib import Path

import pytest
from typer.testing import CliRunner

from glow.cli import app
from glow.lint import lint_manifest
from tests.conftest import TOOLPACKS

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFESTS = sorted(TOOLPACKS.glob("*/manifest.yaml"))
DIGEST = "sha256:" + "0" * 64

runner = CliRunner()


def lint(*paths: Path) -> tuple[int, str, str]:
    result = runner.invoke(app, ["toolpack", "lint", *(str(path) for path in paths)])
    return result.exit_code, result.stdout, result.stderr


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(text)
    return path


def manifest_text(image: str = f"ghcr.io/x/pack@{DIGEST}", tools: str = "") -> str:
    tools = tools or "  - { name: pack.tool, description: d, command: [run] }\n"
    return f"toolpack: pack\nversion: 1\nimage: {image}\ntools:\n{tools}"


def test_committed_manifests_pass() -> None:
    code, stdout, stderr = lint(*MANIFESTS)
    assert code == 0, stderr
    assert stderr == ""
    assert stdout.splitlines() == [f"ok: {path}" for path in MANIFESTS]


def test_missing_media_types_entry_fails() -> None:
    path = FIXTURES / "bad_media_types/manifest.yaml"
    code, _, stderr = lint(path)
    assert code == 1
    assert f"{path}: error: $.tools[0]: output 'result' has no media_types entry for: PNG" in stderr


def test_one_bad_manifest_fails_the_run() -> None:
    code, stdout, _ = lint(MANIFESTS[0], FIXTURES / "bad_media_types/manifest.yaml")
    assert code == 1
    assert f"ok: {MANIFESTS[0]}" in stdout


def test_media_type_from_needs_enum_input(tmp_path: Path) -> None:
    tools = (
        "  - name: pack.tool\n    description: d\n    command: [run]\n"
        "    inputs: { format: { type: string } }\n"
        "    outputs:\n      r: { type: file, media_type_from: format, "
        "media_types: { PNG: image/png } }\n"
    )
    findings = lint_manifest(write(tmp_path, manifest_text(tools=tools)))
    assert [str(f) for f in findings] == [
        "error: $.tools[0]: output 'r' takes media_type_from input 'format' without enum"
    ]


def test_local_image_warns(tmp_path: Path) -> None:
    path = write(tmp_path, manifest_text(image="local/pack:dev"))
    code, stdout, stderr = lint(path)
    assert code == 0
    assert "warning: $.image: local/pack:dev is a local development image" in stderr
    assert "--allow-local-images" in stderr
    assert f"ok: {path}" in stdout


@pytest.mark.parametrize(
    ("image", "message"),
    [
        ("local/other:dev", "local image must be 'local/pack:dev'"),
        ("ghcr.io/x/pack:latest", "does not match"),
        ("local/pack:latest", "does not match"),
    ],
)
def test_image_must_be_digest_or_local_dev(tmp_path: Path, image: str, message: str) -> None:
    code, _, stderr = lint(write(tmp_path, manifest_text(image=image)))
    assert code == 1
    assert message in stderr


def test_misnamed_and_duplicate_tools(tmp_path: Path) -> None:
    misnamed = "  - { name: other.tool, description: d, command: [run] }\n"
    assert "must be named 'pack.<tool>'" in lint(write(tmp_path, manifest_text(tools=misnamed)))[2]
    duplicate = "  - { name: pack.tool, description: d, command: [run] }\n" * 2
    assert (
        "duplicate tool name 'pack.tool'"
        in lint(write(tmp_path, manifest_text(tools=duplicate)))[2]
    )


def test_tool_without_command(tmp_path: Path) -> None:
    tools = "  - { name: pack.tool, description: d }\n"
    code, _, stderr = lint(write(tmp_path, manifest_text(tools=tools)))
    assert code == 1
    assert "tool 'pack.tool' must set command" in stderr


def test_builtin_name_is_an_error(tmp_path: Path) -> None:
    text = "toolpack: fs\nversion: 1\nimage: local/fs:dev\ntools:\n"
    text += "  - { name: fs.glob, description: d, command: [run] }\n"
    code, _, stderr = lint(write(tmp_path, text))
    assert code == 1
    assert "error: $.tools[0].name: 'fs.glob' is a built-in name" in stderr


def test_workflow_file_is_not_a_manifest() -> None:
    code, _, stderr = lint(TOOLPACKS.parent / "examples/sst-ingest.yaml")
    assert code == 1
    assert "$.toolpack: required property is missing" in stderr


def test_unreadable_manifest(tmp_path: Path) -> None:
    code, _, stderr = lint(tmp_path / "missing.yaml")
    assert code == 2
    assert "cannot read" in stderr
