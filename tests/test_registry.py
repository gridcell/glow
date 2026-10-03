import re
from pathlib import Path

import pytest

from glow.builtins import BUILTINS
from glow.lock import LockError, lock_path, write_lock
from glow.manifests import load_manifest
from glow.registry import (
    MalformedUsesError,
    Registry,
    UnknownMajorError,
    UnknownToolError,
    parse_uses,
)
from tests.conftest import TOOLPACKS

DIGEST = "sha256:" + "0" * 64


@pytest.fixture(scope="module")
def registry() -> Registry:
    return Registry.load(TOOLPACKS)


def pin_gdal_digest(toolpacks: Path) -> None:
    """Point the gdal manifest at a digest image and regenerate the lock."""
    path = toolpacks / "gdal/manifest.yaml"
    text = path.read_text()
    image_line = next(line for line in text.splitlines() if line.startswith("image:"))
    path.write_text(text.replace(image_line, f"image: ghcr.io/sparkgeo/glow-gdal@{DIGEST}"))
    write_lock(toolpacks)


def test_resolve_manifest_tool(registry: Registry) -> None:
    resolved = registry.resolve("gdal.translate@1")
    assert resolved.tool.name == "gdal.translate"
    assert resolved.toolpack == "gdal"
    assert resolved.manifest_sha256 == load_manifest(TOOLPACKS / "gdal/manifest.yaml").sha256
    assert not resolved.builtin


def test_resolve_digest_image(toolpacks: Path) -> None:
    pin_gdal_digest(toolpacks)
    resolved = Registry.load(toolpacks).resolve("gdal.translate@1")
    assert resolved.image == "ghcr.io/sparkgeo/glow-gdal"
    assert resolved.digest == DIGEST
    assert resolved.image_ref == f"ghcr.io/sparkgeo/glow-gdal@{DIGEST}"
    assert not resolved.is_local


def test_unknown_major_names_available_majors(registry: Registry) -> None:
    with pytest.raises(UnknownMajorError, match="no major version 2; available: 1") as info:
        registry.resolve("gdal.translate@2")
    assert info.value.available == [1]


@pytest.mark.parametrize("name", ["fs.group", "fs.glob"])
def test_resolve_builtin(registry: Registry, name: str) -> None:
    resolved = registry.resolve(name)
    assert resolved.tool is BUILTINS[name]
    assert resolved.builtin
    assert resolved.image_ref is None
    assert not resolved.is_local


def test_unknown_tool(registry: Registry) -> None:
    with pytest.raises(UnknownToolError, match=re.escape("unknown tool 'gdal.nope'")):
        registry.resolve("gdal.nope@1")


@pytest.mark.parametrize(
    ("uses", "message"),
    [
        ("fs.group@1", "built-in 'fs.group' takes no @major"),
        ("gdal.translate", "needs a major version"),
        ("gdal", "malformed uses"),
        ("GDAL.translate@1", "malformed uses"),
        ("gdal.translate@0", "malformed uses"),
        ("gdal.translate@01", "malformed uses"),
        ("gdal.translate@1\n", "malformed uses"),
        ("gdal.translate@latest", "malformed uses"),
        ("../gdal.translate@1", "malformed uses"),
        ("a." + "b" * 200 + "@1", "malformed uses"),
    ],
)
def test_malformed_uses(registry: Registry, uses: str, message: str) -> None:
    with pytest.raises(MalformedUsesError, match=message):
        registry.resolve(uses)


def test_parse_uses() -> None:
    assert parse_uses("gdal.dem.color_relief@12") == ("gdal.dem.color_relief", 12)
    assert parse_uses("fs.group") == ("fs.group", None)


def test_builtin_specs() -> None:
    group = BUILTINS["fs.group"]
    assert group.command is None
    assert set(group.inputs) == {"root", "pattern", "key", "media_type"}
    assert group.required == ["root", "pattern", "key"]
    items = group.outputs["groups"].items
    assert items is not None and items.type == "group"
    assert items.properties is not None and set(items.properties) == {"key", "captures", "files"}
    glob = BUILTINS["fs.glob"]
    assert set(glob.inputs) == {"root", "pattern"}
    files = glob.outputs["files"]
    assert files.type == "array" and files.items is not None and files.items.type == "file"


def test_hash_ignores_comments_and_layout(toolpacks: Path) -> None:
    path = toolpacks / "gdal/manifest.yaml"
    before = load_manifest(path).sha256
    path.write_text("# extra comment\n\n" + path.read_text().replace("tools:", "tools:   "))
    assert load_manifest(path).sha256 == before


def test_load_fails_on_stale_lock(toolpacks: Path) -> None:
    path = toolpacks / "gdal/manifest.yaml"
    path.write_text(path.read_text().replace("Report raster metadata", "Report metadata"))
    with pytest.raises(LockError, match=re.escape("does not match gdal/manifest.yaml")):
        Registry.load(toolpacks)


def test_load_fails_on_hand_edited_digest(toolpacks: Path) -> None:
    pin_gdal_digest(toolpacks)
    path = lock_path(toolpacks)
    path.write_text(path.read_text().replace(DIGEST, "sha256:" + "1" * 64, 1))
    with pytest.raises(LockError, match="does not match"):
        Registry.load(toolpacks)


def test_load_fails_without_lock(toolpacks: Path) -> None:
    lock_path(toolpacks).unlink()
    with pytest.raises(LockError, match="cannot read"):
        Registry.load(toolpacks)


@pytest.mark.parametrize("manifest", ["../outside/manifest.yaml", "/etc/manifest.yaml", "gdal/x"])
def test_lock_rejects_paths_outside_root(toolpacks: Path, manifest: str) -> None:
    path = lock_path(toolpacks)
    path.write_text(
        path.read_text().replace("manifest: gdal/manifest.yaml", f"manifest: {manifest}")
    )
    with pytest.raises(LockError, match="manifest"):
        Registry.load(toolpacks)


def test_lock_rejects_symlink_out_of_root(toolpacks: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.yaml").write_text((toolpacks / "gdal/manifest.yaml").read_text())
    (toolpacks / "evil").symlink_to(outside)
    path = lock_path(toolpacks)
    path.write_text(
        path.read_text().replace("manifest: gdal/manifest.yaml", "manifest: evil/manifest.yaml")
    )
    with pytest.raises(LockError, match="outside"):
        Registry.load(toolpacks)


def test_local_image_resolves_without_digest(registry: Registry) -> None:
    resolved = registry.resolve("stac.item@1")
    assert resolved.is_local
    assert resolved.digest is None
    assert resolved.image_ref == "local/stac:dev"
