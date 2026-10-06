"""examples/sst-ingest.yaml with the real toolpack images.

Needs docker, glow-exec and the images from `make images`. Skipped without
them, unless GLOW_IMAGE_TESTS=1 is set, as in the CI job that builds them.
The netCDF fixtures are generated in the gdal image into
tests/fixtures/data/sst/, where `glow run` can use them too (docs/runner.md).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from glow.runner import RunOptions, resolve_inputs, run_workflow
from glow.runner.docker import Docker
from tests.compile.conftest import EXAMPLES
from tests.conftest import ROOT, TOOLPACKS
from tests.runner.conftest import load_ir

pytestmark = pytest.mark.integration

SST_DATA = ROOT / "tests" / "fixtures" / "data" / "sst"
IMAGES = ("local/gdal:dev", "local/stac:dev", "local/prescient:dev")
DAYS = ("20240101", "20240102")


@pytest.fixture(scope="module")
def images(docker: Docker) -> None:
    for image in IMAGES:
        result = subprocess.run(
            [docker.docker, "image", "inspect", image], capture_output=True, check=False, timeout=60
        )
        if result.returncode != 0:
            reason = f"{image} is missing; run make images"
            if os.environ.get("GLOW_IMAGE_TESTS") == "1":
                pytest.fail(reason)
            pytest.skip(reason)


@pytest.fixture(scope="module")
def sst_data(docker: Docker, images: None) -> Path:
    result = subprocess.run(
        [
            docker.docker,
            "run",
            "--rm",
            "--network=none",
            f"--user={os.getuid()}:{os.getgid()}",
            f"--mount=type=bind,source={SST_DATA},target=/data",
            "local/gdal:dev",
            "python3",
            "/data/make_fixtures.py",
            "/data",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    return SST_DATA


def test_sst_ingest_publishes_one_item_per_file(
    docker: Docker, sst_data: Path, tmp_path: Path
) -> None:
    workflow = load_ir(EXAMPLES / "sst-ingest.yaml", TOOLPACKS)
    dest = tmp_path / "sst-out"
    inputs = resolve_inputs(
        workflow,
        {
            "source": str(sst_data),
            "dest": str(dest),
            "collection": "noaa-sst",
            "color_table": str(sst_data / "colors.txt"),
        },
    )
    options = RunOptions(run_prefix=str(tmp_path / "prefix"), run_id="sst")
    result = run_workflow(workflow, inputs, options, docker)

    assert result.outputs["publish"]["outputs"]["published"] == len(DAYS)
    for day in DAYS:
        item = json.loads((dest / "noaa-sst" / f"sst-{day}.json").read_text())
        assert item["type"] == "Feature"
        assert set(item["assets"]) >= {"data", "thumbnail"}
        # The fixtures cover -130..-120 E, 45..50 N; the footprint comes from the COG.
        assert item["bbox"] == pytest.approx([-130.0, 45.0, -120.0, 50.0])
        assert item["geometry"]["type"] == "Polygon"
        for asset in ("data", "thumbnail"):
            href = item["assets"][asset]["href"]
            assert Path(href.removeprefix("file://")).is_file(), href
    groups = result.outputs["items"]["outputs"]["groups"]
    # Staging keeps the original basenames, so the cog step saw sst_<day>.nc.
    assert [Path(group["files"][0]["uri"]).name for group in groups] == [
        f"sst_{day}.nc" for day in DAYS
    ]
