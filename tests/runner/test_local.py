"""Scheduling, fan-in, `if` and the run layout, with glow-exec faked in Python."""

import json
from pathlib import Path
from typing import Any

import pytest

from glow import ir
from glow.compile.encoding import decode_json
from glow.runner import RunError, RunOptions, StepFailedError, run_workflow
from glow.runner.local import MAX_ENV_VALUE, expression_variables
from tests.runner.conftest import FAKE_TOOLPACKS, FAKE_WORKFLOW, FakeGlowExec, load_ir


@pytest.fixture
def workflow() -> ir.Workflow:
    return load_ir(FAKE_WORKFLOW, FAKE_TOOLPACKS)


def run(
    workflow: ir.Workflow,
    tmp_path: Path,
    executor: FakeGlowExec,
    names: list[Any],
    **inputs: Any,
) -> tuple[Path, dict[str, Any]]:
    values = {"names": names, "greeting": "hello", "extra": False, **inputs}
    options = RunOptions(run_prefix=str(tmp_path / "prefix"), run_id="test-run")
    result = run_workflow(workflow, values, options, executor)
    assert result.run_id == "test-run"
    return tmp_path / "prefix" / "runs" / "test-run", result.outputs


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def test_runs_steps_fans_out_and_fans_in(workflow: ir.Workflow, tmp_path: Path) -> None:
    executor = FakeGlowExec()
    _, outputs = run(workflow, tmp_path, executor, ["alice", "bob"])

    assert list(outputs) == ["header", "per_name", "joined", "extra"]
    lines = outputs["per_name"]["outputs"]["lines"]
    assert [Path(line["uri"]).name for line in lines] == ["alice.txt", "bob.txt"]
    joined = outputs["joined"]["outputs"]
    assert joined["count"] == 2
    assert Path(joined["result"]["uri"]).read_text() == "hello alice\nhello bob\n"
    # Two block items of two members and three top-level steps. glow-exec
    # evaluates the `if` of `extra`, as in a cluster.
    assert len(executor.calls) == 7


def test_if_false_skips_the_step(workflow: ir.Workflow, tmp_path: Path) -> None:
    run_dir, outputs = run(workflow, tmp_path, FakeGlowExec(), ["alice"])
    assert outputs["extra"] == {"skipped": True}
    assert read(run_dir / "steps" / "extra" / "outputs.resolved.json") == {"skipped": True}


def test_if_true_runs_the_step(workflow: ir.Workflow, tmp_path: Path) -> None:
    _, outputs = run(workflow, tmp_path, FakeGlowExec(), ["alice"], extra=True)
    assert outputs["extra"]["skipped"] is False


def test_every_step_keeps_its_resolved_outputs(workflow: ir.Workflow, tmp_path: Path) -> None:
    run_dir, outputs = run(workflow, tmp_path, FakeGlowExec(), ["alice", "bob"])
    steps = run_dir / "steps"
    for step in ("header", "per_name", "joined", "extra"):
        assert read(steps / step / "outputs.resolved.json") == outputs[step]
    for who in ("alice", "bob"):
        for member in ("line", "copy"):
            resolved = read(steps / "per_name" / who / member / "outputs.resolved.json")
            assert resolved["skipped"] is False
            assert (steps / "per_name" / who / member / "log.txt").is_file()
    # Outputs land under the step's run prefix, as glow-exec uploads them.
    assert outputs["header"]["outputs"]["result"]["uri"] == str(
        steps / "header" / "result" / "header.txt"
    )
    run_json = read(run_dir / "run.json")
    assert run_json["status"] == "succeeded"
    assert run_json["workflow"] == "fake-pipeline"


def test_parameters_match_the_compiled_contract(workflow: ir.Workflow, tmp_path: Path) -> None:
    executor = FakeGlowExec()
    run(workflow, tmp_path, executor, ["alice"])
    by_tool = {}
    for spec in executor.calls:
        by_tool.setdefault(spec.tool, []).append(spec)
    line = next(
        spec
        for spec in by_tool["fake.write@1"]
        if "per_name" in spec.environment["GLOW_RUN_PREFIX"]
    )
    environment = line.environment
    assert decode_json(environment["GLOW_RAW_WITH"]) == workflow.step("line").raw_with
    assert decode_json(environment["GLOW_MANIFEST"]) == workflow.step("line").tool_spec
    assert json.loads(environment["GLOW_SCOPE"])["who"] == "alice"
    assert environment["GLOW_IF"] == ""
    assert environment["GLOW_STAGING"] == "copy"
    assert line.image == "local/fake:dev"
    assert line.command == ["/usr/local/bin/write.sh"]
    copy = by_tool["fake.copy@1"][0]
    assert set(_upstream(copy.environment)) == {"line"}
    joined = by_tool["fake.concat@1"][0]
    upstream = json.loads(joined.environment["GLOW_UPSTREAM_per_name"])
    assert upstream["skipped"] is False
    assert [Path(f["uri"]).name for f in upstream["outputs"]["lines"]] == ["alice.txt"]
    conditions = [spec.environment["GLOW_IF"] for spec in by_tool["fake.write@1"]]
    # The bare CEL text, as the compiler passes it.
    assert sorted(conditions) == ["", "", "inputs.extra"]


def _upstream(environment: dict[str, str]) -> list[str]:
    return [key.removeprefix("GLOW_UPSTREAM_") for key in environment if "UPSTREAM" in key]


def test_items_get_their_index_when_names_are_not_path_safe(
    workflow: ir.Workflow, tmp_path: Path
) -> None:
    run_dir, outputs = run(workflow, tmp_path, FakeGlowExec(), ["same", "same"])
    steps = run_dir / "steps" / "per_name"
    assert (steps / "0" / "line" / "outputs.resolved.json").is_file()
    assert (steps / "1" / "line" / "outputs.resolved.json").is_file()
    assert len(outputs["per_name"]["outputs"]["lines"]) == 2


def test_empty_for_each_fans_in_an_empty_array(workflow: ir.Workflow, tmp_path: Path) -> None:
    _, outputs = run(workflow, tmp_path, FakeGlowExec(), [])
    assert outputs["per_name"] == {"outputs": {"lines": []}, "skipped": False}
    assert outputs["joined"]["outputs"]["count"] == 0


def test_a_failed_step_stops_the_run(workflow: ir.Workflow, tmp_path: Path) -> None:
    executor = FakeGlowExec(fail=frozenset({"fake.copy"}))
    with pytest.raises(StepFailedError) as caught:
        run(workflow, tmp_path, executor, ["alice", "bob"])
    assert caught.value.step in {"per_name[alice].copy", "per_name[bob].copy"}
    assert "failing on purpose" in (caught.value.log or "")
    run_dir = tmp_path / "prefix" / "runs" / "test-run"
    assert read(run_dir / "run.json")["status"] == "failed"
    # Steps that finished keep their outputs; the consumer never ran.
    assert (run_dir / "steps" / "header" / "outputs.resolved.json").is_file()
    assert not (run_dir / "steps" / "joined").exists()
    assert all(spec.tool != "fake.concat@1" for spec in executor.calls)


def test_large_parameters_go_to_a_file(workflow: ir.Workflow, tmp_path: Path) -> None:
    executor = FakeGlowExec()
    big = "x" * (MAX_ENV_VALUE + 1)
    run(workflow, tmp_path, executor, ["alice"], greeting=big)
    header = executor.calls[0]
    assert "GLOW_SCOPE" not in header.environment
    assert list(header.flags) == ["--scope", "@/work/params/GLOW_SCOPE.json"]


def test_work_directories_are_removed_unless_kept(workflow: ir.Workflow, tmp_path: Path) -> None:
    values = {"names": ["alice"], "greeting": "hi", "extra": False}
    for keep in (False, True):
        options = RunOptions(
            run_prefix=str(tmp_path / "prefix"), keep_work=keep, work_root=tmp_path
        )
        result = run_workflow(workflow, values, options, FakeGlowExec())
        assert (result.work_dir is not None) == keep
    kept = [path for path in tmp_path.iterdir() if path.name.startswith("glow-")]
    assert len(kept) == 1
    assert (kept[0] / "per_name" / "alice" / "line").is_dir()


def test_block_if_false_skips_every_member(tmp_path: Path) -> None:
    path = tmp_path / "workflow.yaml"
    text = FAKE_WORKFLOW.read_text().replace(
        "    as: who\n", "    as: who\n    if: ${{ inputs.extra }}\n"
    )
    path.write_text(text)
    workflow = load_ir(path, FAKE_TOOLPACKS)
    executor = FakeGlowExec()
    values = {"names": ["alice", "bob"], "greeting": "hi", "extra": False}
    options = RunOptions(run_prefix=str(tmp_path / "prefix"), run_id="skip")
    with pytest.raises(StepFailedError, match="joined"):
        # As in Argo, the fan-in holds null for each skipped item, which the
        # consumer then rejects.
        run_workflow(workflow, values, options, executor)
    resolved = read(tmp_path / "prefix/runs/skip/steps/per_name/outputs.resolved.json")
    assert resolved == {"outputs": {"lines": [None, None]}, "skipped": False}
    member = tmp_path / "prefix/runs/skip/steps/per_name/alice/line/outputs.resolved.json"
    assert read(member) == {"skipped": True}


def test_rejects_what_the_compiler_rejects(tmp_path: Path) -> None:
    path = tmp_path / "workflow.yaml"
    path.write_text(
        FAKE_WORKFLOW.read_text().replace(
            "          text: ${{ inputs.greeting }} ${{ who }}",
            "          text: hi ${{ path.basename(steps.header.outputs.result.uri) }}",
        )
    )
    workflow = load_ir(path, FAKE_TOOLPACKS)
    with pytest.raises(RunError, match="GLOW-E050"):
        run_workflow(
            workflow,
            {"names": [], "greeting": "x", "extra": False},
            RunOptions(run_prefix=str(tmp_path)),
            FakeGlowExec(),
        )


def test_run_options_reject_bad_values() -> None:
    with pytest.raises(ValueError, match="run id"):
        RunOptions(run_prefix="/tmp/x", run_id="../escape")
    with pytest.raises(ValueError, match="parallelism"):
        RunOptions(run_prefix="/tmp/x", max_parallelism=0)
    with pytest.raises(ValueError, match="absolute"):
        RunOptions(run_prefix="relative/dir")


def test_expression_variables_mirror_glow_exec() -> None:
    file = {"uri": "/a/b.txt", "kind": "file"}
    variables = expression_variables(
        {"inputs": {"f": file}},
        {"a": {"outputs": {"r": file}, "skipped": False}, "b": {"skipped": True}},
    )
    assert variables["inputs"]["f"]["path"] == "/a/b.txt"
    assert variables["steps"]["a"]["outputs"]["r"]["path"] == "/a/b.txt"
    assert variables["steps"]["b"] == {"outputs": {}, "skipped": True}
    # The caller's values are not changed.
    assert "path" not in file
