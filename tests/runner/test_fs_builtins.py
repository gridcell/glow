"""fs.group and fs.glob over a local directory, and the engine's tool contract."""

import json
import os
from pathlib import Path

import pytest

from glow.builtins import BuiltinError, run_builtin, run_in_work_dir
from glow.builtins.fs import MAX_PATTERN_LENGTH, glob_to_regex

SST_PATTERN = r"sst_(?P<date>\d{8})\.nc$"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    for relative in (
        "sst_20240102.nc",
        "sst_20240101.nc",
        "nested/sst_20240103.nc",
        "colors.txt",
        "sst_2024.nc",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return root


def test_group_matches_the_sst_pattern(tree: Path) -> None:
    result = run_builtin(
        "fs.group",
        {
            "root": str(tree),
            "pattern": SST_PATTERN,
            "key": "date",
            "media_type": "application/x-netcdf",
        },
    )
    groups = result["groups"]
    assert [group["key"] for group in groups] == ["20240101", "20240102", "20240103"]
    first = groups[0]
    assert first["captures"] == {"date": "20240101"}
    assert first["files"] == [
        {
            "uri": f"{tree}/sst_20240101.nc",
            "kind": "file",
            "media_type": "application/x-netcdf",
        }
    ]
    # The URI keeps the original basename, so staging preserves it.
    assert groups[2]["files"][0]["uri"] == f"{tree}/nested/sst_20240103.nc"


def test_group_collects_every_file_of_a_key(tmp_path: Path) -> None:
    for name in ("b_1.tif", "a_1.tif", "a_2.tif"):
        (tmp_path / name).write_bytes(b"")
    result = run_builtin(
        "fs.group", {"root": f"file://{tmp_path}", "pattern": r"(?P<band>\w)_(?P<n>\d)", "key": "n"}
    )
    groups = {group["key"]: group for group in result["groups"]}
    assert [Path(f["uri"]).name for f in groups["1"]["files"]] == ["a_1.tif", "b_1.tif"]
    assert groups["1"]["captures"] == {"band": "a", "n": "1"}
    assert groups["1"]["files"][0]["uri"].startswith("file://")
    assert "media_type" not in groups["1"]["files"][0]


def test_group_skips_symbolic_links(tree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sst_20249999.nc").write_bytes(b"")
    os.symlink(outside, tree / "link")
    os.symlink(outside / "sst_20249999.nc", tree / "sst_20249998.nc")
    result = run_builtin("fs.group", {"root": str(tree), "pattern": SST_PATTERN, "key": "date"})
    assert "20249999" not in {group["key"] for group in result["groups"]}
    assert "20249998" not in {group["key"] for group in result["groups"]}


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"pattern": "x", "key": "k"}, "'root' is required"),
        ({"root": "/r", "pattern": "x", "key": "k", "extra": 1}, "not declared"),
        ({"root": "/r", "pattern": 5, "key": "k"}, "must be a string"),
        ({"root": "/r", "pattern": "(?P<a>x)", "key": "b"}, "no named capture 'b'"),
        ({"root": "/r", "pattern": "(", "key": "k"}, "not a valid regular expression"),
        ({"root": "/r", "pattern": "x" * (MAX_PATTERN_LENGTH + 1), "key": "k"}, "longer"),
        ({"root": "relative", "pattern": "(?P<k>x)", "key": "k"}, "not an absolute path"),
        ({"root": "http://host/x", "pattern": "(?P<k>x)", "key": "k"}, "not an absolute path"),
        ({"root": "/does/not/exist", "pattern": "(?P<k>x)", "key": "k"}, "not a directory"),
    ],
)
def test_group_rejects_bad_inputs(inputs: dict, message: str) -> None:
    with pytest.raises(BuiltinError, match=message):
        run_builtin("fs.group", inputs)


def test_glob(tree: Path) -> None:
    result = run_builtin("fs.glob", {"root": str(tree), "pattern": "**/sst_*.nc"})
    names = [file["uri"].removeprefix(f"{tree}/") for file in result["files"]]
    assert names == ["nested/sst_20240103.nc", "sst_2024.nc", "sst_20240101.nc", "sst_20240102.nc"]
    top = run_builtin("fs.glob", {"root": str(tree), "pattern": "*.txt"})
    assert [file["kind"] for file in top["files"]] == ["file"]


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("*.tif", "a.tif", True),
        ("*.tif", "d/a.tif", False),
        ("**/*.tif", "a.tif", True),
        ("**/*.tif", "d/e/a.tif", True),
        ("d/**", "d/e/a.tif", True),
        ("a?.tif", "ab.tif", True),
        ("a?.tif", "a/.tif", False),
        ("a+b.tif", "a+b.tif", True),
    ],
)
def test_glob_translation(pattern: str, path: str, matches: bool) -> None:
    import re

    assert bool(re.fullmatch(glob_to_regex(pattern), path)) is matches


def test_unknown_builtin() -> None:
    with pytest.raises(BuiltinError, match="not a built-in"):
        run_builtin("fs.nope", {})


def test_engine_contract_reads_inputs_and_writes_outputs(tree: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    inputs = {"root": str(tree), "pattern": SST_PATTERN, "key": "date"}
    (work / "inputs.json").write_text(json.dumps(inputs))
    run_in_work_dir("fs.group", work)
    outputs = json.loads((work / "outputs.json").read_text())
    assert len(outputs["groups"]) == 3


def test_engine_contract_needs_inputs(tmp_path: Path) -> None:
    with pytest.raises(BuiltinError, match=r"inputs\.json"):
        run_in_work_dir("fs.group", tmp_path)
