import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from glow.cli import app
from glow.lock import LockError, build_lock, lock_is_current, lock_path, render_lock, write_lock
from tests.conftest import TOOLPACKS

runner = CliRunner()


def lock(*args: str) -> tuple[int, str, str]:
    result = runner.invoke(app, ["toolpack", "lock", *args])
    return result.exit_code, result.stdout, result.stderr


def test_committed_lock_is_current() -> None:
    assert lock_is_current(TOOLPACKS)


def test_lock_keys_cover_every_tool() -> None:
    assert list(build_lock(TOOLPACKS).tools) == [
        "gdal.dem.color_relief@1",
        "gdal.info@1",
        "gdal.translate@1",
        "prescient.render_from_color_table@1",
        "stac.download@1",
        "stac.item@1",
        "stac.publish@1",
        "stac.search@1",
    ]


def test_regenerate_is_idempotent(toolpacks: Path) -> None:
    before = lock_path(toolpacks).read_text()
    write_lock(toolpacks)
    assert lock_path(toolpacks).read_text() == before
    assert render_lock(build_lock(toolpacks)) == before


def test_check_passes_on_committed_lock() -> None:
    code, stdout, stderr = lock("--check", "--root", str(TOOLPACKS))
    assert code == 0, stderr
    assert "is up to date" in stdout


def test_check_fails_after_manifest_edit_until_relocked(toolpacks: Path) -> None:
    path = toolpacks / "gdal/manifest.yaml"
    path.write_text(path.read_text().replace("default: COG", "default: GTiff"))
    code, _, stderr = lock("--check", "--root", str(toolpacks))
    assert code == 1
    assert "does not match the manifests" in stderr
    code, stdout, _ = lock("--root", str(toolpacks))
    assert code == 0 and "wrote" in stdout
    code, _, stderr = lock("--check", "--root", str(toolpacks))
    assert code == 0, stderr


def test_check_fails_when_lock_missing(toolpacks: Path) -> None:
    lock_path(toolpacks).unlink()
    code, _, stderr = lock("--check", "--root", str(toolpacks))
    assert code == 1
    assert "does not match the manifests" in stderr


def test_version_bump_changes_keys(toolpacks: Path) -> None:
    path = toolpacks / "stac/manifest.yaml"
    path.write_text(path.read_text().replace("version: 1", "version: 2"))
    keys = set(build_lock(toolpacks).tools)
    assert {"stac.item@2", "stac.publish@2"} <= keys
    assert "stac.item@1" not in keys


def test_duplicate_tool_major_across_manifests(toolpacks: Path) -> None:
    copy = toolpacks / "gdal_copy"
    copy.mkdir()
    (copy / "manifest.yaml").write_text((toolpacks / "gdal/manifest.yaml").read_text())
    with pytest.raises(LockError, match=re.escape("also provided by gdal/manifest.yaml")):
        build_lock(toolpacks)


def test_builtin_name_is_reserved(toolpacks: Path) -> None:
    pack = toolpacks / "fs"
    pack.mkdir()
    (pack / "manifest.yaml").write_text(
        "toolpack: fs\nversion: 1\nimage: local/fs:dev\n"
        "tools:\n  - { name: fs.group, description: d, command: [run] }\n"
    )
    with pytest.raises(LockError, match="name of a built-in"):
        build_lock(toolpacks)


def test_invalid_manifest_is_reported(toolpacks: Path) -> None:
    path = toolpacks / "gdal/manifest.yaml"
    path.write_text(path.read_text().replace("version: 1", "version: one"))
    code, _, stderr = lock("--check", "--root", str(toolpacks))
    assert code == 1
    assert "gdal/manifest.yaml: $.version: expected integer, got string" in stderr


def test_missing_root(tmp_path: Path) -> None:
    code, _, stderr = lock("--root", str(tmp_path / "nope"))
    assert code == 1
    assert "is not a directory" in stderr
