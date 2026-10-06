import json
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pystac
import pytest
from conftest import PRESCIENT_IMAGE, STAC_IMAGE, RunTool

RENDER_SCHEMA = "https://stac-extensions.github.io/render/v2.0.0/schema.json"
RENDERS = {"sst": {"assets": ["data"], "rescale": [[0, 30]], "colormap_name": "viridis"}}


def item_inputs(**overrides: Any) -> dict[str, Any]:
    inputs = {
        "id": "sst-20240101",
        "datetime": "2024-01-01T00:00:00Z",
        "assets": {
            "data": {
                "href": {
                    "uri": "/work/in/source/raster.tif",
                    "media_type": "image/tiff; application=geotiff; profile=cloud-optimized",
                    "kind": "file",
                },
                "roles": ["data"],
                "title": "Sea surface temperature",
            }
        },
        "asset_entries": [
            {"key": "thumbnail", "href": "s3://bucket/run/thumb/out.png", "roles": ["thumbnail"]}
        ],
        "extensions": {"render": RENDERS},
    }
    return {**inputs, **overrides}


def validate(document: dict[str, Any]) -> None:
    """Validate against the STAC JSON Schemas, which pystac downloads."""
    try:
        pystac.Item.from_dict(document).validate()
    except URLError as exc:
        pytest.skip(f"STAC schemas are not reachable: {exc}")


def test_item_merges_assets_and_render_extension(run_tool: RunTool) -> None:
    run = run_tool(STAC_IMAGE, "stac_item_wrapper", item_inputs()).check()
    assert sorted(path.name for path in run.out.iterdir()) == ["item.json"]
    item = json.loads((run.out / "item.json").read_text())
    assert item["stac_version"] == "1.0.0"
    assert item["id"] == "sst-20240101"
    assert item["properties"]["datetime"] == "2024-01-01T00:00:00Z"
    assert set(item["assets"]) == {"data", "thumbnail"}
    data = item["assets"]["data"]
    assert data["href"] == "/work/in/source/raster.tif"
    assert data["type"] == "image/tiff; application=geotiff; profile=cloud-optimized"
    assert item["assets"]["thumbnail"]["href"] == "s3://bucket/run/thumb/out.png"
    assert item["bbox"] == pytest.approx([-130.0, 45.0, -120.0, 50.0])
    assert item["geometry"]["type"] == "Polygon"
    assert RENDER_SCHEMA in item["stac_extensions"]
    assert item["properties"]["renders"] == RENDERS
    validate(item)


def test_item_entry_wins_over_map(run_tool: RunTool) -> None:
    entries = [{"key": "data", "href": "/work/in/source/other.tif", "type": "image/tiff"}]
    run = run_tool(STAC_IMAGE, "stac_item_wrapper", item_inputs(asset_entries=entries)).check()
    item = json.loads((run.out / "item.json").read_text())
    assert item["assets"] == {"data": {"href": "/work/in/source/other.tif", "type": "image/tiff"}}
    # The only asset is not a readable raster, so the item has no footprint.
    assert item["geometry"] is None
    assert "bbox" not in item
    validate(item)


def test_item_rejects_unknown_extension(run_tool: RunTool) -> None:
    run = run_tool(STAC_IMAGE, "stac_item_wrapper", item_inputs(extensions={"eo": {}}))
    assert run.result.returncode != 0
    assert "unknown extension 'eo'" in run.result.stderr


def write_item(path: Path, item_id: str) -> None:
    when = pystac.utils.str_to_datetime("2024-01-01T00:00:00Z")
    item = pystac.Item(item_id, geometry=None, bbox=None, datetime=when, properties={})
    path.write_text(json.dumps(item.to_dict(include_self_link=False)))


def test_publish_writes_one_file_per_item_and_index(run_tool: RunTool, work: Path) -> None:
    write_item(work / "in" / "a.json", "sst-20240101")
    write_item(work / "in" / "b.json", "sst-20240102")
    items = ["/work/in/a.json", {"uri": "file:///work/in/b.json", "kind": "file"}]
    inputs = {"collection": "sst", "items": items, "dest": "file:///work/catalog"}
    run = run_tool(STAC_IMAGE, "stac_publish_wrapper", inputs).check()
    assert run.outputs() == {"published": 2}
    collection = work / "catalog" / "sst"
    assert sorted(path.name for path in collection.iterdir()) == [
        "items.json",
        "sst-20240101.json",
        "sst-20240102.json",
    ]
    index = json.loads((collection / "items.json").read_text())
    assert [feature["id"] for feature in index["features"]] == ["sst-20240101", "sst-20240102"]

    # Publishing again replaces the item and keeps the others in the index.
    rerun = {**inputs, "items": ["/work/in/a.json"]}
    assert run_tool(STAC_IMAGE, "stac_publish_wrapper", rerun).check().outputs() == {"published": 1}
    assert len(json.loads((collection / "items.json").read_text())["features"]) == 2


@pytest.mark.parametrize(
    ("item_id", "collection", "dest", "message"),
    [
        ("../escape", "sst", "/work/catalog", "unsafe item id"),
        ("ok", "../escape", "/work/catalog", "invalid collection id"),
        ("ok", "sst", "s3://bucket/catalog", "not a local path"),
        ("items", "sst", "/work/catalog", "would replace"),
    ],
)
def test_publish_rejects_unsafe_targets(
    run_tool: RunTool, work: Path, item_id: str, collection: str, dest: str, message: str
) -> None:
    write_item(work / "in" / "a.json", item_id)
    inputs = {"collection": collection, "items": ["/work/in/a.json"], "dest": dest}
    run = run_tool(STAC_IMAGE, "stac_publish_wrapper", inputs)
    assert run.result.returncode != 0
    assert message in run.result.stderr
    assert not (work / "catalog").exists()


def test_render_from_color_table(run_tool: RunTool) -> None:
    inputs = {"color_table": "/work/in/source/color_table.txt", "asset": "data", "name": "sst"}
    run = run_tool(PRESCIENT_IMAGE, "render_from_color_table_wrapper", inputs).check()
    render = run.outputs()["renders"]["sst"]
    assert render["assets"] == ["data"]
    assert render["rescale"] == [[0.0, 30.0]]
    colormap = render["colormap"]
    assert len(colormap) == 256
    assert colormap["0"] == [0, 0, 255, 255]
    assert colormap["255"] == [255, 0, 0, 255]
    # Step 85 is value 10, two thirds of the way from blue (0) to green (15).
    assert colormap["85"] == [0, 170, 85, 255]


def test_render_rejects_percent_rows(run_tool: RunTool, work: Path) -> None:
    (work / "in" / "table.txt").write_text("0% 0 0 0\n100% 255 255 255\n")
    inputs = {"color_table": "/work/in/table.txt", "asset": "data", "name": "sst"}
    run = run_tool(PRESCIENT_IMAGE, "render_from_color_table_wrapper", inputs)
    assert run.result.returncode != 0
    assert "line 1" in run.result.stderr
