import yaml
from typer.testing import CliRunner

from glow.cli import app
from tests.compile.conftest import EXAMPLES, FIXTURES, template
from tests.conftest import TOOLPACKS

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, ["compile", *args, "--toolpacks", str(TOOLPACKS)])


def test_compile_writes_the_workflow(tmp_path) -> None:
    out = tmp_path / "sst.yaml"
    result = invoke(str(EXAMPLES / "sst-ingest.yaml"), "--allow-local-images", "-o", str(out))
    assert result.exit_code == 0, result.stderr
    assert result.stdout == f"wrote {out}\n"
    workflow = yaml.safe_load(out.read_text())
    assert workflow["metadata"] == {"generateName": "sst-ingest-"}
    assert workflow["spec"]["serviceAccountName"] == "glow-runner"


def test_compile_options_reach_the_workflow() -> None:
    result = invoke(
        str(FIXTURES / "script-step.yaml"),
        "--namespace",
        "tenant-a",
        "--service-account",
        "runner-a",
        "--run-prefix",
        "s3://bucket-a/glow/",
        "--glow-exec-image",
        "registry.example/glow-exec:1",
        "--engine-image",
        "registry.example/engine:1",
        "--sandbox-image",
        "registry.example/sandbox:1",
    )
    assert result.exit_code == 0, result.stderr
    workflow = yaml.safe_load(result.stdout)
    assert workflow["metadata"]["namespace"] == "tenant-a"
    assert workflow["spec"]["serviceAccountName"] == "runner-a"
    score = template(workflow, "script-score")
    assert score["container"]["image"] == "registry.example/sandbox:1"
    assert score["initContainers"][0]["image"] == "registry.example/glow-exec:1"
    assert "s3://bucket-a/glow/runs/{{workflow.uid}}/steps/score" in result.stdout


def test_local_images_fail_without_the_flag() -> None:
    result = invoke(str(EXAMPLES / "sst-ingest.yaml"))
    assert result.exit_code == 1
    assert "error: cog.uses [GLOW-E051]" in result.stderr
    assert "5 problem(s) found" in result.stderr


def test_outer_reference_in_a_block_compiles() -> None:
    result = invoke(str(FIXTURES / "block-outer-step-output.yaml"), "--allow-local-images")
    assert result.exit_code == 0, result.stderr
    assert "upstream-items" in result.stdout


def test_invalid_workflow_fails_validation() -> None:
    result = invoke(str(FIXTURES.parent / "cycle.yaml"))
    assert result.exit_code == 1
    assert "GLOW-E020" in result.stderr


def test_bad_option_exits_2() -> None:
    result = invoke(str(EXAMPLES / "sst-ingest.yaml"), "--run-prefix", "relative")
    assert result.exit_code == 2
    assert "run prefix 'relative'" in result.stderr


def test_unreadable_file_exits_2(tmp_path) -> None:
    assert invoke(str(tmp_path / "missing.yaml")).exit_code == 2


def test_manifest_is_not_a_workflow() -> None:
    result = invoke(str(TOOLPACKS / "gdal" / "manifest.yaml"))
    assert result.exit_code == 1
    assert "not a workflow" in result.stderr
