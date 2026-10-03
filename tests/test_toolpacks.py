from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from glow.models import ToolpackManifest
from glow.schema_export import load_schema

TOOLPACKS = Path(__file__).resolve().parent.parent / "toolpacks"
MANIFESTS = sorted(TOOLPACKS.glob("*/manifest.yaml"))
DIGEST = "sha256:" + "0" * 64


def test_expected_manifests_exist() -> None:
    assert [path.parent.name for path in MANIFESTS] == ["gdal", "prescient", "stac"]


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda path: path.parent.name)
def test_manifest_matches_model_and_schema(path: Path) -> None:
    data = yaml.safe_load(path.read_text())
    manifest = ToolpackManifest.model_validate(data)
    assert manifest.toolpack == path.parent.name
    Draft202012Validator(load_schema("toolpack.schema.json")).validate(data)


def test_gdal_tools() -> None:
    manifest = ToolpackManifest.model_validate(
        yaml.safe_load((TOOLPACKS / "gdal/manifest.yaml").read_text())
    )
    tools = {tool.name: tool for tool in manifest.tools}
    assert set(tools) == {"gdal.translate", "gdal.dem.color_relief", "gdal.info"}
    result = tools["gdal.translate"].outputs["result"]
    assert result.media_type_from == "format"
    assert result.media_types is not None and set(result.media_types) == {"COG", "GTiff", "PNG"}


def manifest(**tool: Any) -> dict[str, Any]:
    base = {"name": "pack.tool", "description": "d", "command": ["run"]}
    return {
        "toolpack": "pack",
        "version": 1,
        "image": f"ghcr.io/x/pack@{DIGEST}",
        "tools": [{**base, **tool}],
    }


def error_messages(data: dict[str, Any]) -> list[str]:
    with pytest.raises(ValidationError) as info:
        ToolpackManifest.model_validate(data)
    return [error["msg"] for error in info.value.errors()]


def test_minimal_manifest_is_valid() -> None:
    ToolpackManifest.model_validate(manifest())


@pytest.mark.parametrize(
    "image",
    ["ghcr.io/x/pack:latest", "ghcr.io/x/pack@sha256:3f1c", f"ghcr.io/x/pack:1.0@{DIGEST}x"],
)
def test_image_must_be_pinned_by_digest(image: str) -> None:
    assert error_messages({**manifest(), "image": image})


def test_tool_name_must_use_toolpack_prefix() -> None:
    assert any(
        "must be named 'pack.<tool>'" in m for m in error_messages(manifest(name="other.tool"))
    )


def test_duplicate_tool_names() -> None:
    data = manifest()
    data["tools"].append(dict(data["tools"][0]))
    assert any("duplicate tool name" in m for m in error_messages(data))


@pytest.mark.parametrize("path", ["/etc/passwd", "../out.tif", "a/../../b", ""])
def test_output_path_must_stay_under_work_out(path: str) -> None:
    data = manifest(outputs={"result": {"type": "file", "path": path}})
    assert any("relative to /work/out/" in m for m in error_messages(data))


def test_media_type_only_on_data_kinds() -> None:
    data = manifest(inputs={"n": {"type": "string", "media_type": "text/plain"}})
    assert any("only allowed on file, bundle or group" in m for m in error_messages(data))


def test_media_type_from_must_name_an_input() -> None:
    output = {"type": "file", "media_type_from": "format", "media_types": {"PNG": "image/png"}}
    assert any(
        "unknown input 'format'" in m for m in error_messages(manifest(outputs={"r": output}))
    )


def test_media_types_must_cover_enum() -> None:
    output = {"type": "file", "media_type_from": "format", "media_types": {"PNG": "image/png"}}
    data = manifest(
        inputs={"format": {"type": "string", "enum": ["PNG", "COG"]}}, outputs={"r": output}
    )
    assert any("no media_types entry for: COG" in m for m in error_messages(data))


def test_required_must_name_inputs() -> None:
    assert any("unknown inputs: nope" in m for m in error_messages(manifest(required=["nope"])))


def test_unknown_input_keyword_is_rejected() -> None:
    assert error_messages(manifest(inputs={"n": {"type": "string", "pattern": ".*"}}))


@pytest.mark.parametrize("version", [None, 0, "1", True])
def test_version_is_a_positive_integer(version: Any) -> None:
    data = manifest()
    if version is None:
        del data["version"]
    else:
        data["version"] = version
    assert error_messages(data)


def test_local_dev_image_is_valid() -> None:
    assert ToolpackManifest.model_validate({**manifest(), "image": "local/pack:dev"}).is_local
    assert not ToolpackManifest.model_validate(manifest()).is_local


def test_items_only_on_array_outputs() -> None:
    data = manifest(outputs={"r": {"type": "file", "items": {"type": "file"}}})
    assert any("items is only allowed on array outputs" in m for m in error_messages(data))
    ToolpackManifest.model_validate(
        manifest(outputs={"r": {"type": "array", "items": {"type": "file"}}})
    )
