"""The runner passes glow-exec the parameters the compiler emits, byte for byte."""

from pathlib import Path
from typing import Any

import pytest

from glow.compile.naming import argo_name
from glow.runner import RunOptions, resolve_inputs, run_workflow
from tests.compile.conftest import EXAMPLES, compiled
from tests.conftest import TOOLPACKS
from tests.runner.conftest import FakeGlowExec, load_ir

SST = EXAMPLES / "sst-ingest.yaml"


@pytest.fixture
def sst_run(tmp_path: Path) -> FakeGlowExec:
    source = tmp_path / "source"
    source.mkdir()
    for day in ("20240101", "20240102"):
        (source / f"sst_{day}.nc").write_bytes(b"")
    colors = tmp_path / "colors.txt"
    colors.write_text("0 0 0 255\n")
    workflow = load_ir(SST, TOOLPACKS)
    given = {
        "source": str(source),
        "dest": str(tmp_path / "dest"),
        "collection": "noaa-sst",
        "color_table": str(colors),
    }
    executor = FakeGlowExec()
    options = RunOptions(run_prefix=str(tmp_path / "prefix"), run_id="parity")
    run_workflow(workflow, resolve_inputs(workflow, given), options, executor)
    return executor


def _compiled_parameters() -> dict[str, dict[str, Any]]:
    """Per task name: the raw-with argument and the GLOW_MANIFEST of its template."""
    workflow = compiled(SST)
    templates = {template["name"]: template for template in workflow["spec"]["templates"]}
    found = {}
    for template in templates.values():
        for task in template.get("dag", {}).get("tasks", []):
            arguments = {
                p["name"]: p.get("value") for p in task.get("arguments", {}).get("parameters", [])
            }
            if "raw-with" not in arguments:
                continue
            container = templates[task["template"]]["container"]
            env = {item["name"]: item.get("value") for item in container["env"]}
            found[task["name"]] = {
                "raw-with": arguments["raw-with"],
                "manifest": env["GLOW_MANIFEST"],
            }
    return found


def test_raw_with_and_manifest_match_the_compiled_workflow(sst_run: FakeGlowExec) -> None:
    expected = _compiled_parameters()
    seen = set()
    for spec in sst_run.calls:
        step = spec.environment["GLOW_RUN_PREFIX"].rsplit("/", 1)[-1]
        task = argo_name(step)
        assert spec.environment["GLOW_RAW_WITH"] == expected[task]["raw-with"], step
        assert spec.environment["GLOW_MANIFEST"] == expected[task]["manifest"], step
        seen.add(step)
    # Every tool step ran; fs.group is a built-in and runs in-process.
    assert seen == {"cog", "thumb", "render", "item", "publish"}
    assert len(sst_run.calls) == 4 * 2 + 1
