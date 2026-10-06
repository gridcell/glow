from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from glow import ir
from glow.compile import CompileOptions, compile_workflow
from glow.validate import validate
from tests.conftest import ROOT, TOOLPACKS

EXAMPLES = ROOT / "examples"
FIXTURES = ROOT / "tests" / "fixtures" / "workflows" / "compile"
GOLDEN = ROOT / "tests" / "compile" / "golden"

# Fixed settings, so golden files do not change when the defaults do.
OPTIONS = CompileOptions(
    namespace="tenant-acme",
    service_account="glow-runner",
    run_prefix="s3://artifact-bucket-acme",
    glow_exec_image="ghcr.io/sparkgeo/glow-exec:test",
    engine_image="ghcr.io/sparkgeo/glow-engine:test",
    sandbox_image="ghcr.io/sparkgeo/glow-sandbox:test",
    allow_local_images=True,
)

# Every workflow with a golden file, by golden file name.
COMPILED = {
    "sst-ingest": EXAMPLES / "sst-ingest.yaml",
    "minimal-if-script": EXAMPLES / "minimal-if-script.yaml",
    "for-each-step": FIXTURES / "for-each-step.yaml",
    "secrets": FIXTURES / "secrets.yaml",
    "resources": FIXTURES / "resources.yaml",
    "script-step": FIXTURES / "script-step.yaml",
    "block-outer-step-output": FIXTURES / "block-outer-step-output.yaml",
    "block-outer-let": FIXTURES / "block-outer-let.yaml",
    "block-outer-loop-var": FIXTURES / "block-outer-loop-var.yaml",
    "block-nested-fan-in": FIXTURES / "block-nested-fan-in.yaml",
}


def load_ir(path: Path) -> ir.Workflow:
    report = validate(path, TOOLPACKS)
    assert report.ir is not None, report.messages()
    return report.ir


def compiled(path: Path, options: CompileOptions = OPTIONS) -> dict:
    """The compiled Workflow of a workflow file, as a plain dict."""
    return yaml.safe_load(compile_workflow(load_ir(path), options).to_yaml())


def template(workflow: dict, name: str) -> dict:
    return next(t for t in workflow["spec"]["templates"] if t["name"] == name)


def task(dag_template: dict, name: str) -> dict:
    return next(t for t in dag_template["dag"]["tasks"] if t["name"] == name)


def arguments(dag_task: dict) -> dict[str, str]:
    parameters = dag_task.get("arguments", {}).get("parameters", [])
    return {p["name"]: p.get("value") for p in parameters}


IrFromYaml = Callable[[str], ir.Workflow]


@pytest.fixture
def ir_from_yaml(tmp_path: Path) -> IrFromYaml:
    """The IR of a workflow given as YAML text."""

    def run(text: str) -> ir.Workflow:
        path = tmp_path / "workflow.yaml"
        path.write_text(text)
        return load_ir(path)

    return run
