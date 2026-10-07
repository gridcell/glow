import json
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pystac
import pytest
from conftest import PRESCIENT_IMAGE, STAC_IMAGE, RunHttpTool, RunTool

RENDER_SCHEMA = "https://stac-extensions.github.io/render/v2.0.0/schema.json"
EO_SCHEMA = "https://stac-extensions.github.io/eo/v1.1.0/schema.json"
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


def test_item_declares_stac_extensions(run_tool: RunTool) -> None:
    inputs = item_inputs(properties={"eo:cloud_cover": 4.5}, stac_extensions=[EO_SCHEMA])
    run = run_tool(STAC_IMAGE, "stac_item_wrapper", inputs).check()
    item = json.loads((run.out / "item.json").read_text())
    assert item["stac_extensions"] == [RENDER_SCHEMA, EO_SCHEMA]
    assert item["properties"]["eo:cloud_cover"] == 4.5
    validate(item)


def test_item_rejects_non_https_extension(run_tool: RunTool) -> None:
    inputs = item_inputs(stac_extensions=["file:///etc/passwd"])
    run = run_tool(STAC_IMAGE, "stac_item_wrapper", inputs)
    assert run.result.returncode != 0
    assert "not an https:// schema URI" in run.result.stderr


def write_item(
    path: Path,
    item_id: str,
    bbox: list[float] | None = None,
    when: str = "2024-01-01T00:00:00Z",
) -> None:
    item = pystac.Item(
        item_id,
        geometry=None,
        bbox=bbox,
        datetime=pystac.utils.str_to_datetime(when),
        properties={},
    )
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
        "collection.json",
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
        ("collection", "sst", "/work/catalog", "would replace"),
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


def test_publish_writes_collection(run_tool: RunTool, work: Path) -> None:
    write_item(work / "in" / "a.json", "s2-a", [-128.0, 48.0, -126.0, 49.0], "2026-05-02T19:00:00Z")
    write_item(
        work / "in" / "b.json", "s2-b", [-125.0, 49.0, -123.0, 50.5], "2026-09-27T19:41:08.5Z"
    )
    inputs = {
        "collection": "s2",
        "items": ["/work/in/a.json", "/work/in/b.json"],
        "dest": "/work/catalog",
        "title": "Sentinel-2",
        "description": "Test scenes.",
        "license": "CC-BY-4.0",
    }
    run_tool(STAC_IMAGE, "stac_publish_wrapper", inputs).check()
    directory = work / "catalog" / "s2"
    collection = json.loads((directory / "collection.json").read_text())
    assert collection["type"] == "Collection"
    assert (collection["id"], collection["title"]) == ("s2", "Sentinel-2")
    assert (collection["description"], collection["license"]) == ("Test scenes.", "CC-BY-4.0")
    assert collection["extent"]["spatial"]["bbox"] == [[-128.0, 48.0, -123.0, 50.5]]
    interval = collection["extent"]["temporal"]["interval"]
    assert interval == [["2026-05-02T19:00:00Z", "2026-09-27T19:41:08.500000Z"]]
    item_links = [link["href"] for link in collection["links"] if link["rel"] == "item"]
    assert item_links == ["./s2-a.json", "./s2-b.json"]

    item = json.loads((directory / "s2-a.json").read_text())
    assert item["collection"] == "s2"
    rels = {link["rel"]: link["href"] for link in item["links"]}
    assert rels == {rel: "./collection.json" for rel in ("root", "parent", "collection")}
    index = json.loads((directory / "items.json").read_text())
    assert [feature["id"] for feature in index["features"]] == ["s2-a", "s2-b"]
    try:
        pystac.Collection.from_dict(collection).validate()
    except URLError as exc:
        pytest.skip(f"STAC schemas are not reachable: {exc}")


def test_publish_collection_defaults(run_tool: RunTool, work: Path) -> None:
    write_item(work / "in" / "a.json", "a")
    inputs = {"collection": "c", "items": ["/work/in/a.json"], "dest": "/work/catalog"}
    run_tool(STAC_IMAGE, "stac_publish_wrapper", inputs).check()
    collection = json.loads((work / "catalog" / "c" / "collection.json").read_text())
    assert collection["license"] == "proprietary"
    assert collection["description"] == "Items of the c collection."
    assert "title" not in collection
    # Items without a bbox give a whole-world extent.
    assert collection["extent"]["spatial"]["bbox"] == [[-180.0, -90.0, 180.0, 90.0]]


def feature(item_id: str, assets: tuple[str, ...] = ("visual", "thumbnail")) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": item_id,
        "collection": "sentinel-2-l2a",
        "bbox": [-128.0, 48.0, -126.0, 49.0],
        "properties": {"datetime": "2026-05-02T19:00:00Z", "eo:cloud_cover": 3.2},
        "assets": {
            key: {"href": f"https://example.com/{item_id}/{key}", "type": "image/tiff", "x": 1}
            for key in assets
        },
        "links": [],
    }


def page(*features: dict[str, Any], next_token: str | None = None) -> dict[str, Any]:
    links = []
    if next_token is not None:
        body = {"collections": ["sentinel-2-l2a"], "next": next_token}
        links.append(
            {"rel": "next", "method": "POST", "href": "https://api.test/search", "body": body}
        )
    return {"type": "FeatureCollection", "features": list(features), "links": links}


def test_search_follows_pages_and_keeps_named_assets(run_http_tool: RunHttpTool) -> None:
    inputs = {
        "api": "https://api.test/",
        "collections": ["sentinel-2-l2a"],
        "bbox": [-128.5, 48.3, -123.2, 50.9],
        "datetime": "2026-05-01T00:00:00Z/2026-09-30T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": 10}},
        "assets": ["visual"],
        "max_items": 3,
    }
    responses = [
        page(feature("a"), feature("b"), next_token="t1"),
        page(feature("c"), feature("d")),
    ]
    run, calls = run_http_tool("stac_search_wrapper", inputs, responses)
    run.check()
    assert [call["url"] for call in calls] == ["https://api.test/search"] * 2
    assert calls[0]["body"] == {
        "collections": ["sentinel-2-l2a"],
        "limit": 3,
        "bbox": [-128.5, 48.3, -123.2, 50.9],
        "datetime": "2026-05-01T00:00:00Z/2026-09-30T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": 10}},
    }
    assert calls[1]["body"]["next"] == "t1"
    outputs = run.outputs()
    assert outputs["count"] == 3
    assert [item["id"] for item in outputs["items"]] == ["a", "b", "c"]
    assert outputs["items"][0] == {
        "id": "a",
        "collection": "sentinel-2-l2a",
        "datetime": "2026-05-02T19:00:00Z",
        "bbox": [-128.0, 48.0, -126.0, 49.0],
        "properties": {"datetime": "2026-05-02T19:00:00Z", "eo:cloud_cover": 3.2},
        "assets": {"visual": {"href": "https://example.com/a/visual", "type": "image/tiff"}},
    }


def test_search_stops_without_a_next_link(run_http_tool: RunHttpTool) -> None:
    run, calls = run_http_tool("stac_search_wrapper", {"collections": ["x"]}, [page(feature("a"))])
    assert run.check().outputs()["count"] == 1
    assert calls[0]["url"] == "https://earth-search.aws.element84.com/v1/search"
    assert calls[0]["body"] == {"collections": ["x"], "limit": 100}


@pytest.mark.parametrize(
    ("inputs", "responses", "message"),
    [
        ({"api": "http://api.test"}, [], "not an https:// URL"),
        ({"assets": ["visual"]}, [page(feature("a", ("red",)))], "no asset named: visual"),
        ({}, [{"status": 400}], "returned HTTP 400"),
        ({}, [{"text": "<html>"}], "cannot search"),
    ],
)
def test_search_failures(
    run_http_tool: RunHttpTool, inputs: dict[str, Any], responses: list[Any], message: str
) -> None:
    run, _ = run_http_tool("stac_search_wrapper", {"collections": ["x"], **inputs}, responses)
    assert run.result.returncode != 0
    assert message in run.result.stderr
    assert not (run.work / "outputs.json").exists()


@pytest.mark.parametrize(("fmt", "name"), [(None, "asset.tif"), ("JP2", "asset.jp2")])
def test_download_writes_asset(run_http_tool: RunHttpTool, fmt: str | None, name: str) -> None:
    inputs = {"href": "https://example.com/TCI.tif", **({"format": fmt} if fmt else {})}
    run, calls = run_http_tool("stac_download_wrapper", inputs, [{"text": "raster bytes"}])
    run.check()
    assert [call["url"] for call in calls] == ["https://example.com/TCI.tif"]
    assert sorted(path.name for path in run.out.iterdir()) == [name]
    assert (run.out / name).read_text() == "raster bytes"


def test_download_retries_server_errors(run_http_tool: RunHttpTool) -> None:
    responses = [{"status": 503}, {"text": "ok"}]
    run, calls = run_http_tool("stac_download_wrapper", {"href": "https://e.com/a"}, responses)
    run.check()
    assert len(calls) == 2
    assert (run.out / "asset.tif").read_text() == "ok"


@pytest.mark.parametrize(
    ("href", "responses", "message"),
    [
        ("http://example.com/a.tif", [], "not an https:// URL"),
        ("file:///etc/passwd", [], "not an https:// URL"),
        ("https://example.com/a.tif", [{"status": 404}], "returned HTTP 404"),
        ("https://example.com/a.tif", [{"status": 500}] * 3, "returned HTTP 500"),
    ],
)
def test_download_failures_leave_no_output(
    run_http_tool: RunHttpTool, href: str, responses: list[Any], message: str
) -> None:
    run, _ = run_http_tool("stac_download_wrapper", {"href": href}, responses)
    assert run.result.returncode != 0
    assert message in run.result.stderr
    assert not any(run.out.iterdir())


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
