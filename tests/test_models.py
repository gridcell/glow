from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from glow.models import Step, ToolInput, Workflow, iter_steps

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((EXAMPLES / name).read_text())


def workflow(*steps: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {"name": "test", "steps": list(steps), **top}


BLOCK = {"id": "a", "for_each": [1], "as": "i", "steps": [{"id": "b", "uses": "x.y"}]}


def errors_of(data: dict[str, Any]) -> list[tuple[tuple[str | int, ...], str, str]]:
    with pytest.raises(ValidationError) as info:
        Workflow.model_validate(data)
    return [(e["loc"], e["type"], e["msg"]) for e in info.value.errors()]


def test_sst_example_loads() -> None:
    wf = Workflow.model_validate(load("sst-ingest.yaml"))
    assert wf.name == "sst-ingest"
    assert wf.inputs["color_table"].media_type == "text/plain"
    assert [step.id for step in iter_steps(wf.steps)] == [
        "items", "per_item", "cog", "thumb", "render", "item", "publish",
    ]  # fmt: skip
    per_item = wf.steps[1]
    assert per_item.kind == "for_each"
    assert per_item.as_ == "g"
    assert per_item.outputs == {"item": "${{ steps.item.outputs.item }}"}
    assert per_item.steps is not None
    assert per_item.steps[0].with_ is not None
    assert per_item.steps[0].with_["creation_options"] == {"COMPRESS": "DEFLATE", "PREDICTOR": "2"}


def test_minimal_example_covers_if_run_and_nested_for_each() -> None:
    wf = Workflow.model_validate(load("minimal-if-script.yaml"))
    count, per_scene, info = wf.steps
    assert count.kind == "run"
    assert count.outputs is not None and count.outputs["total"].type == "integer"
    assert per_scene.if_ == "${{ steps.count.outputs.total > 0 }}"
    assert per_scene.steps is not None
    assert per_scene.steps[0].kind == "for_each"
    assert per_scene.steps[0].steps is not None
    assert per_scene.steps[0].steps[0].timeout == "30m"
    assert info.kind == "for_each" and info.uses == "gdal.info@1"


@pytest.mark.parametrize(
    ("step", "kind"),
    [
        ({"id": "a", "uses": "gdal.info@1"}, "uses"),
        ({"id": "a", "run": "echo", "outputs": {"x": {"type": "string"}}}, "run"),
        ({"id": "a", "script": "print()", "outputs": {"x": {"type": "file"}}}, "script"),
        ({"id": "a", "for_each": [1], "as": "i", "uses": "fs.glob"}, "for_each"),
    ],
)
def test_step_kind(step: dict[str, Any], kind: str) -> None:
    assert Step.model_validate(step).kind == kind


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"id": "a"}, "exactly one of uses, run, script, for_each (found: none)"),
        ({"id": "a", "uses": "x.y", "run": "echo"}, "(found: uses, run)"),
        ({"id": "a", "for_each": [1], "uses": "x.y"}, "for_each requires 'as'"),
        ({"id": "a", "for_each": [1], "as": "i"}, "found: none"),
        ({**BLOCK, "uses": "x.y"}, "not both"),
        ({"id": "a", "uses": "x.y", "as": "i"}, "'as' is only allowed on a for_each step"),
        ({"id": "a", "uses": "x.y", "max_parallelism": 2}, "'max_parallelism' is only allowed"),
        ({"id": "a", "run": "echo"}, "must declare 'outputs'"),
        ({"id": "a", "run": "echo", "outputs": {"x": "${{ 1 }}"}}, "must declare a type"),
        ({"id": "a", "uses": "x.y", "outputs": {"x": "${{ 1 }}"}}, "not allowed on a uses step"),
        ({**BLOCK, "outputs": {"x": {"type": "string"}}}, "block outputs must be expressions"),
    ],
)
def test_invalid_step_shapes(step: dict[str, Any], message: str) -> None:
    [(loc, error_type, msg)] = errors_of(workflow(step))
    assert loc == ("steps", 0)
    assert error_type == "glow_step_shape"
    assert message in msg


def test_nested_step_error_location() -> None:
    nested = {**BLOCK, "steps": [{"id": "b", "uses": "x.y", "run": "x"}]}
    [(loc, error_type, _)] = errors_of(workflow(nested))
    assert loc == ("steps", 0, "steps", 0)
    assert error_type == "glow_step_shape"


def test_unknown_top_level_key() -> None:
    [(loc, error_type, _)] = errors_of(workflow({"id": "a", "uses": "x.y"}, schedule="daily"))
    assert loc == ("schedule",)
    assert error_type == "extra_forbidden"


def test_step_requires_id() -> None:
    [(loc, error_type, _)] = errors_of(workflow({"uses": "x.y"}))
    assert loc == ("steps", 0, "id")
    assert error_type == "missing"


def test_duplicate_step_ids_across_scopes() -> None:
    [(_, error_type, msg)] = errors_of(workflow(BLOCK, {"id": "b", "uses": "x.y"}))
    assert error_type == "glow_duplicate_step_id"
    assert "'b'" in msg


@pytest.mark.parametrize(
    "step",
    [
        {"id": "has-hyphen", "uses": "x.y"},
        {"id": "a", "uses": "gdal.translate@latest"},
        {"id": "a", "uses": "x.y", "staging": "stream"},
        {"id": "a", "uses": "x.y", "timeout": "5x"},
        {"id": "a", "uses": "x.y", "timeout": 0},
        {"id": "a", "uses": "x.y", "retries": -1},
        {"id": "a", "uses": "x.y", "secrets": ["Not_A_K8s_Name"]},
        {"id": "a", "uses": "x.y", "resources": {"requests": {"memory": "lots"}}},
        {"id": "a", "uses": "x.y", "resources": {"requests": {"disk": "1Gi"}}},
    ],
)
def test_invalid_step_fields(step: dict[str, Any]) -> None:
    assert errors_of(workflow(step))


def test_input_media_type_requires_data_kind() -> None:
    data = workflow(
        {"id": "a", "uses": "x.y"}, inputs={"n": {"type": "string", "media_type": "text/plain"}}
    )
    [(loc, error_type, _)] = errors_of(data)
    assert loc == ("inputs", "n")
    assert error_type == "glow_input_media_type"


def test_input_category_requires_data_kind_and_a_known_name() -> None:
    assert ToolInput(type="file", category=["raster", "vector"]).category == ["raster", "vector"]
    with pytest.raises(ValidationError, match="category is only allowed on file"):
        ToolInput(type="string", category="raster")
    with pytest.raises(ValidationError):
        ToolInput(type="file", category="lidar")
