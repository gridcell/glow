import pytest

from glow import ir
from glow.types import Array, Bundle, File, Group, MediaType, Scalar, Unknown
from glow.validate import validate
from tests.conftest import TOOLPACKS
from tests.validate.conftest import EXAMPLES, FIXTURES

TIFF = MediaType.parse("image/tiff; application=geotiff")


@pytest.mark.parametrize(
    "path",
    [
        EXAMPLES / "sst-ingest.yaml",
        EXAMPLES / "minimal-if-script.yaml",
        FIXTURES / "group-without-media-type.yaml",
    ],
)
def test_ir_round_trips_through_json(path) -> None:
    workflow = validate(path, TOOLPACKS).ir
    assert workflow is not None
    assert ir.Workflow.model_validate_json(workflow.model_dump_json()) == workflow


@pytest.mark.parametrize(
    "glow_type",
    [
        Scalar({"type": "object", "properties": {"a": {"type": "string"}}}),
        File((TIFF,)),
        File(unknown_reason="later"),
        Bundle((MediaType.parse("*"),)),
        Group((TIFF,), None, ("date",)),
        Array(Array(Unknown("why"))),
    ],
)
def test_type_codec_round_trips(glow_type) -> None:
    assert ir.decode_type(ir.encode_type(glow_type)) == glow_type


@pytest.mark.parametrize("value", [None, [], {"kind": "tensor"}])
def test_decode_rejects_unknown_shapes(value) -> None:
    with pytest.raises(ValueError):
        ir.decode_type(value)


def test_sst_ir_content() -> None:
    workflow = validate(EXAMPLES / "sst-ingest.yaml", TOOLPACKS).ir
    assert workflow is not None
    assert [step.id for step in workflow.steps] == [
        "items",
        "per_item",
        "cog",
        "thumb",
        "render",
        "item",
        "publish",
    ]
    cog = workflow.step("cog")
    assert cog.parent == "per_item"
    assert cog.tool is not None and cog.tool.uses == "gdal.translate@1"
    assert cog.tool.toolpack == "gdal" and cog.tool.manifest_sha256
    per_item = workflow.step("per_item")
    assert per_item.loop is not None
    assert per_item.loop.item == Group((MediaType.parse("application/x-netcdf"),))
    assert per_item.outputs == {"item": Array(File((MediaType.parse("application/geo+json"),)))}
    assert workflow.step("publish").depends_on == ["per_item"]
    assert workflow.step("item").depends_on == ["cog", "thumb", "render"]


def test_ir_carries_what_the_compiler_needs() -> None:
    workflow = validate(EXAMPLES / "sst-ingest.yaml", TOOLPACKS).ir
    assert workflow is not None
    cog = workflow.step("cog")
    assert cog.raw_with["source"] == "${{ g.files[0].path }}"
    assert cog.tool_spec is not None and cog.tool_spec["command"] == ["gdal_translate_wrapper"]
    per_item = workflow.step("per_item")
    assert per_item.loop is not None
    assert per_item.loop.operand == "${{ steps.items.outputs.groups }}"
    assert per_item.block_outputs == {"item": "${{ steps.item.outputs.item }}"}
    assert per_item.tool_spec is None


def test_ir_synthesizes_a_tool_spec_for_run_steps() -> None:
    workflow = validate(EXAMPLES / "minimal-if-script.yaml", TOOLPACKS).ir
    assert workflow is not None
    assert workflow.defaults == {"make_previews": True}
    count = workflow.step("count")
    assert count.run is not None and count.run.startswith("python - <<'PY'")
    assert count.tool_spec == {
        "name": "glow.run",
        "inputs": {"scenes": {"type": "array"}},
        "outputs": {"total": {"type": "integer", "description": "Number of scenes"}},
    }
