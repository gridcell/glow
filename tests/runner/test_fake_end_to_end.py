"""The fake pipeline end to end: once with glow-exec faked, once in Docker.

Both runs must produce the recorded baseline, `fake/baseline.json`: every
step's outputs.resolved.json, with the run directory as `$RUN`. A cluster run
of the same workflow is compared against it for parity (plan section 6.4).
Refresh it with UPDATE_BASELINE=1 and review the diff.
"""

import json
import os
from pathlib import Path

from glow.runner import RunOptions, run_workflow
from glow.runner.docker import Docker
from tests.runner.conftest import (
    FAKE,
    FAKE_TOOLPACKS,
    FAKE_WORKFLOW,
    FakeGlowExec,
    load_ir,
    resolved_tree,
)

BASELINE = FAKE / "baseline.json"
INPUTS = {"names": ["alice", "bob"], "greeting": "hello", "extra": False}


def run_fake_pipeline(tmp_path: Path, executor: FakeGlowExec | Docker) -> Path:
    workflow = load_ir(FAKE_WORKFLOW, FAKE_TOOLPACKS)
    options = RunOptions(run_prefix=str(tmp_path / "prefix"), run_id="e2e")
    result = run_workflow(workflow, INPUTS, options, executor)
    joined = result.outputs["joined"]["outputs"]
    assert joined["count"] == 2
    assert Path(joined["result"]["uri"]).read_text() == "hello alice\nhello bob\n"
    assert result.outputs["extra"] == {"skipped": True}
    return tmp_path / "prefix" / "runs" / "e2e"


def test_fake_run_matches_the_baseline(tmp_path: Path) -> None:
    tree = resolved_tree(run_fake_pipeline(tmp_path, FakeGlowExec()))
    if os.environ.get("UPDATE_BASELINE") == "1":
        BASELINE.write_text(json.dumps(tree, indent=2, sort_keys=True) + "\n")
    assert tree == json.loads(BASELINE.read_text())


def test_docker_run_matches_the_baseline(tmp_path: Path, docker: Docker, fake_image: str) -> None:
    run_dir = run_fake_pipeline(tmp_path, docker)
    assert resolved_tree(run_dir) == json.loads(BASELINE.read_text())
    for who in ("alice", "bob"):
        copied = run_dir / "steps" / "per_name" / who / "copy" / "result" / f"{who}.txt"
        assert copied.read_text() == f"hello {who}\n"
        assert (
            "glow-exec" in (run_dir / "steps" / "per_name" / who / "copy" / "log.txt").read_text()
        )
