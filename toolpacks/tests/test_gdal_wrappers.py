import struct
from pathlib import Path

from conftest import GDAL_IMAGE, GdalInfo, RunTool

NETCDF = "/work/in/source/sst_20240101.nc"
GEOTIFF = "/work/in/source/raster.tif"
COLOR_TABLE = "/work/in/source/color_table.txt"


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from the PNG IHDR chunk."""
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", header[16:24])


def test_translate_netcdf_subdataset_to_cog(run_tool: RunTool, gdalinfo: GdalInfo) -> None:
    inputs = {
        "source": NETCDF,
        "subdataset": "sea_surface_temperature",
        "format": "COG",
        "creation_options": {"COMPRESS": "DEFLATE"},
    }
    run = run_tool(GDAL_IMAGE, "gdal_translate_wrapper", inputs).check()
    assert sorted(path.name for path in run.out.iterdir()) == ["out.tif"]
    info = gdalinfo("/work/out/out.tif")
    structure = info["metadata"]["IMAGE_STRUCTURE"]
    assert structure["LAYOUT"] == "COG"
    assert structure["COMPRESSION"] == "DEFLATE"
    assert info["size"] == [20, 10]


def test_translate_defaults_to_cog(run_tool: RunTool, gdalinfo: GdalInfo) -> None:
    run_tool(GDAL_IMAGE, "gdal_translate_wrapper", {"source": GEOTIFF}).check()
    assert gdalinfo("/work/out/out.tif")["metadata"]["IMAGE_STRUCTURE"]["LAYOUT"] == "COG"


def test_translate_png_extension(run_tool: RunTool) -> None:
    run = run_tool(GDAL_IMAGE, "gdal_translate_wrapper", {"source": GEOTIFF, "format": "PNG"})
    assert (run.check().out / "out.png").is_file()


def test_translate_rejects_quoted_subdataset(run_tool: RunTool) -> None:
    inputs = {"source": NETCDF, "subdataset": 'x":/etc/passwd'}
    run = run_tool(GDAL_IMAGE, "gdal_translate_wrapper", inputs)
    assert run.result.returncode != 0
    assert "invalid subdataset" in run.result.stderr
    assert not any(run.out.iterdir())


def test_translate_rejects_creation_option_injection(run_tool: RunTool) -> None:
    inputs = {"source": GEOTIFF, "creation_options": {"COMPRESS=NONE -of": "PNG"}}
    run = run_tool(GDAL_IMAGE, "gdal_translate_wrapper", inputs)
    assert run.result.returncode != 0
    assert "invalid creation option" in run.result.stderr


def test_color_relief_resizes_png(run_tool: RunTool) -> None:
    inputs = {"source": GEOTIFF, "color_table": COLOR_TABLE, "size": [600, 0], "format": "PNG"}
    run = run_tool(GDAL_IMAGE, "gdaldem_color_relief_wrapper", inputs).check()
    assert sorted(path.name for path in run.out.iterdir()) == ["out.png"]
    assert png_size(run.out / "out.png") == (600, 300)


def test_color_relief_keeps_size_by_default(run_tool: RunTool, gdalinfo: GdalInfo) -> None:
    inputs = {"source": GEOTIFF, "color_table": COLOR_TABLE, "format": "GTiff"}
    run_tool(GDAL_IMAGE, "gdaldem_color_relief_wrapper", inputs).check()
    info = gdalinfo("/work/out/out.tif")
    assert info["size"] == [20, 10]
    assert len(info["bands"]) == 4  # RGBA


def test_color_relief_rejects_zero_size(run_tool: RunTool) -> None:
    inputs = {"source": GEOTIFF, "color_table": COLOR_TABLE, "size": [0, 0]}
    run = run_tool(GDAL_IMAGE, "gdaldem_color_relief_wrapper", inputs)
    assert run.result.returncode != 0
    assert "size" in run.result.stderr


def test_info_writes_scalar_output(run_tool: RunTool) -> None:
    run = run_tool(GDAL_IMAGE, "gdalinfo_wrapper", {"source": GEOTIFF, "stats": True}).check()
    info = run.outputs()["info"]
    assert info["size"] == [20, 10]
    assert info["bands"][0]["maximum"] == 28.5
    assert list(run.outputs()) == ["info"]


def test_info_fails_on_missing_source(run_tool: RunTool) -> None:
    run = run_tool(GDAL_IMAGE, "gdalinfo_wrapper", {"source": "/work/in/source/missing.tif"})
    assert run.result.returncode != 0
    assert not (run.work / "outputs.json").exists()
