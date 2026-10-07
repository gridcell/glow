"""Run toolpack wrappers inside their images, following the /work contract.

The tests need docker and the images from `make images`. Without them the
tests are skipped, unless GLOW_IMAGE_TESTS=1 is set, as in CI, where a
missing image is an error.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
GDAL_IMAGE = "local/gdal:dev"
STAC_IMAGE = "local/stac:dev"
PRESCIENT_IMAGE = "local/prescient:dev"
REQUIRED = os.environ.get("GLOW_IMAGE_TESTS") == "1"
TIMEOUT = 300


def docker_command() -> str:
    docker = shutil.which("docker")
    if docker is None:
        unavailable("docker is not installed")
    for image in (GDAL_IMAGE, STAC_IMAGE, PRESCIENT_IMAGE):
        result = subprocess.run(
            [docker, "image", "inspect", image], capture_output=True, check=False, timeout=60
        )
        if result.returncode != 0:
            unavailable(f"{image} is missing; run make images")
    return docker


def unavailable(reason: str) -> NoReturn:
    if REQUIRED:
        pytest.fail(reason)
    pytest.skip(reason)


@dataclass
class ToolRun:
    """One tool run: its work directory and the process result."""

    work: Path
    result: subprocess.CompletedProcess[str]

    @property
    def out(self) -> Path:
        return self.work / "out"

    def outputs(self) -> dict[str, Any]:
        return json.loads((self.work / "outputs.json").read_text())

    def check(self) -> "ToolRun":
        assert self.result.returncode == 0, self.result.stderr
        return self


@pytest.fixture(scope="session")
def docker() -> str:
    return docker_command()


def run_in_image(
    docker: str, image: str, work: Path, command: list[str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--network=none",
            f"--user={os.getuid()}:{os.getgid()}",
            "--env=GLOW_WORK_DIR=/work",
            f"--volume={work}:/work",
            image,
            *command,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT,
    )


@pytest.fixture(scope="session")
def rasters(docker: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """sst_20240101.nc and raster.tif, generated in the gdal image."""
    work = tmp_path_factory.mktemp("fixtures")
    shutil.copy(HERE / "make_fixtures.py", work / "make_fixtures.py")
    result = run_in_image(
        docker, GDAL_IMAGE, work, ["python3", "/work/make_fixtures.py", "/work/rasters"]
    )
    assert result.returncode == 0, result.stderr
    return work / "rasters"


@pytest.fixture
def work(tmp_path: Path, rasters: Path) -> Path:
    """A work directory with the fixtures staged under in/source/."""
    work = tmp_path / "work"
    source = work / "in" / "source"
    shutil.copytree(rasters, source)
    shutil.copy(FIXTURES / "color_table.txt", source / "color_table.txt")
    (work / "out").mkdir()
    return work


RunTool = Callable[[str, str, dict[str, Any]], ToolRun]
GdalInfo = Callable[[str], dict[str, Any]]


@pytest.fixture
def run_tool(docker: str, work: Path) -> RunTool:
    """Write inputs.json, run `command` in `image` and return the run."""

    def run(image: str, command: str, inputs: dict[str, Any]) -> ToolRun:
        (work / "inputs.json").write_text(json.dumps(inputs))
        return ToolRun(work, run_in_image(docker, image, work, [command]))

    return run


RunHttpTool = Callable[[str, dict[str, Any], list[Any]], tuple[ToolRun, list[dict[str, Any]]]]


@pytest.fixture
def run_http_tool(docker: str, work: Path) -> RunHttpTool:
    """Run a stac image wrapper with canned HTTP responses (see fake_http.py).

    Returns the run and the HTTP calls the wrapper made.
    """

    def run(command: str, inputs: dict[str, Any], responses: list[Any]) -> tuple[ToolRun, Any]:
        (work / "inputs.json").write_text(json.dumps(inputs))
        (work / "responses.json").write_text(json.dumps(responses))
        shutil.copy(HERE / "fake_http.py", work / "fake_http.py")
        driver = [
            "python3",
            "/work/fake_http.py",
            f"/usr/local/bin/{command}",
            "/work/responses.json",
            "/work/calls.json",
        ]
        result = run_in_image(docker, STAC_IMAGE, work, driver)
        calls = json.loads((work / "calls.json").read_text())
        return ToolRun(work, result), calls

    return run


@pytest.fixture
def gdalinfo(docker: str, work: Path) -> GdalInfo:
    """`gdalinfo -json` of a path in the work directory, run in the gdal image."""

    def info(path: str) -> dict[str, Any]:
        result = run_in_image(docker, GDAL_IMAGE, work, ["gdalinfo", "-json", path])
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return info
