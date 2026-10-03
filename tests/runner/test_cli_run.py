"""`glow run` and `glow builtin` from the command line."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from glow.cli import app
from tests.compile.conftest import EXAMPLES
from tests.conftest import TOOLPACKS

runner = CliRunner()

BUILTINS_ONLY = """
name: list-files
inputs:
  source: { type: uri }
steps:
  - id: listing
    uses: fs.glob
    with:
      root: ${{ inputs.source }}
      pattern: "**/*.txt"
"""


def test_dry_run_prints_the_plan() -> None:
    result = runner.invoke(
        app, ["run", str(EXAMPLES / "sst-ingest.yaml"), "--toolpacks", str(TOOLPACKS), "--dry-run"]
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("workflow sst-ingest:")


def test_bad_inputs_are_a_usage_error() -> None:
    result = runner.invoke(
        app,
        ["run", str(EXAMPLES / "sst-ingest.yaml"), "--toolpacks", str(TOOLPACKS), "-i", "x=1"],
    )
    assert result.exit_code == 2
    assert "input 'x' is not declared" in result.stderr
    assert "input 'source' is required" in result.stderr


def test_bad_run_prefix_is_a_usage_error(tmp_path: Path) -> None:
    path = tmp_path / "workflow.yaml"
    path.write_text(BUILTINS_ONLY)
    result = runner.invoke(
        app, ["run", str(path), "-i", f"source={tmp_path}", "--run-prefix", "http://x/y"]
    )
    assert result.exit_code == 2
    assert "run prefix" in result.stderr


def test_runs_a_workflow_of_builtins_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "a").mkdir(parents=True)
    (tmp_path / "data" / "a" / "one.txt").write_text("1")
    (tmp_path / "workflow.yaml").write_text(BUILTINS_ONLY)
    result = runner.invoke(app, ["run", "workflow.yaml", "--input", "source=data"])
    assert result.exit_code == 0, result.stderr
    assert "ok: listing" in result.stdout
    resolved_files = list(
        (tmp_path / ".glow" / "runs").glob("*/steps/listing/outputs.resolved.json")
    )
    assert len(resolved_files) == 1
    resolved = json.loads(resolved_files[0].read_text())
    assert resolved == {
        "outputs": {"files": [{"uri": f"{tmp_path}/data/a/one.txt", "kind": "file"}]},
        "skipped": False,
    }


def test_a_failing_builtin_fails_the_run(tmp_path: Path) -> None:
    (tmp_path / "workflow.yaml").write_text(BUILTINS_ONLY)
    (tmp_path / "file.txt").write_text("")
    result = runner.invoke(
        app,
        [
            "run",
            str(tmp_path / "workflow.yaml"),
            "-i",
            f"source={tmp_path}/file.txt",
            "--run-prefix",
            str(tmp_path / "prefix"),
        ],
    )
    assert result.exit_code == 1
    assert "step listing failed" in result.stderr
    assert "not a directory" in result.stderr


def test_builtin_command_follows_the_tool_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "x.txt").write_text("")
    work = tmp_path / "work"
    work.mkdir()
    (work / "inputs.json").write_text(json.dumps({"root": str(tmp_path), "pattern": "*.txt"}))
    monkeypatch.setenv("GLOW_WORK_DIR", str(work))
    result = runner.invoke(app, ["builtin", "fs.glob"])
    assert result.exit_code == 0, result.stderr
    outputs = json.loads((work / "outputs.json").read_text())
    assert [Path(file["uri"]).name for file in outputs["files"]] == ["x.txt"]
    failed = runner.invoke(app, ["builtin", "fs.nope"])
    assert failed.exit_code == 1
    assert "not a built-in" in failed.stderr
