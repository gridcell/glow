"""Reading `--input name=value` against the declared input types."""

from pathlib import Path

import pytest

from glow import ir
from glow.runner.inputs import (
    InputError,
    LocalPath,
    local_paths,
    parse_assignments,
    resolve_inputs,
)
from tests.conftest import TOOLPACKS
from tests.runner.conftest import load_ir

WORKFLOW = """
name: inputs
inputs:
  source: { type: uri }
  table: { type: file, media_type: text/plain }
  tiles: { type: bundle }
  label: { type: string }
  count: { type: integer, default: 3 }
  ratio: { type: number, default: 0.5 }
  flag: { type: boolean, default: false }
  names: { type: array, default: [] }
  options: { type: object, default: {} }
steps:
  - id: list
    uses: fs.glob
    with:
      root: ${{ inputs.source }}
      pattern: "*"
"""


@pytest.fixture
def workflow(tmp_path: Path) -> ir.Workflow:
    path = tmp_path / "workflow.yaml"
    path.write_text(WORKFLOW)
    return load_ir(path, TOOLPACKS)


@pytest.fixture
def files(tmp_path: Path) -> Path:
    (tmp_path / "table.txt").write_text("0 0 0 0\n")
    (tmp_path / "tiles").mkdir()
    return tmp_path


def given(files: Path, **values: str) -> dict[str, str]:
    return {
        "source": "data",
        "table": str(files / "table.txt"),
        "tiles": f"file://{files}/tiles",
        "label": "x",
        **values,
    }


def test_parse_assignments() -> None:
    assert parse_assignments(["a=1", "b=x=y", "c="]) == {"a": "1", "b": "x=y", "c": ""}
    with pytest.raises(InputError, match="name=value"):
        parse_assignments(["novalue"])
    with pytest.raises(InputError, match="more than once"):
        parse_assignments(["a=1", "a=2"])


def test_values_follow_the_declared_types(workflow: ir.Workflow, files: Path) -> None:
    values = resolve_inputs(
        workflow, given(files, count="7", flag="true", names='["a"]'), cwd=files
    )
    assert values["source"] == str(files / "data")
    assert values["table"] == {
        "uri": str(files / "table.txt"),
        "kind": "file",
        "media_type": "text/plain",
    }
    assert values["tiles"] == {"uri": str(files / "tiles"), "kind": "bundle"}
    assert values["label"] == "x"
    assert values["count"] == 7
    assert values["flag"] is True
    assert values["names"] == ["a"]
    # Defaults fill what is not given.
    assert values["ratio"] == 0.5
    assert values["options"] == {}


def test_s3_uris_pass_through(workflow: ir.Workflow, files: Path) -> None:
    values = resolve_inputs(
        workflow, given(files, source="s3://bucket/sst/", tiles="s3://bucket/tiles"), cwd=files
    )
    assert values["source"] == "s3://bucket/sst/"
    assert values["tiles"]["uri"] == "s3://bucket/tiles"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"count": "1.5"}, "not a valid integer"),
        ({"count": "true"}, "not a valid integer"),
        ({"flag": "yes"}, "not valid JSON"),
        ({"names": "{}"}, "not a valid array"),
        ({"source": "http://example.com/x"}, "local path, file:// URI or s3:// URI"),
        ({"source": "s3:///key"}, "no bucket"),
        ({"source": ""}, "empty"),
        ({"table": "missing.txt"}, "not an existing file"),
        ({"tiles": "table.txt"}, "not an existing directory"),
        ({"unknown": "1"}, "not declared"),
    ],
)
def test_rejects_bad_values(
    workflow: ir.Workflow, files: Path, override: dict[str, str], message: str
) -> None:
    with pytest.raises(InputError, match=message):
        resolve_inputs(workflow, given(files, **override), cwd=files)


def test_required_inputs(workflow: ir.Workflow, files: Path) -> None:
    values = given(files)
    del values["label"]
    with pytest.raises(InputError, match="'label' is required"):
        resolve_inputs(workflow, values, cwd=files)


def test_local_paths(workflow: ir.Workflow, files: Path) -> None:
    values = resolve_inputs(workflow, given(files), cwd=files)
    assert local_paths(workflow, values) == [
        LocalPath(files / "data", writable=True),
        LocalPath(files / "table.txt", writable=False),
        LocalPath(files / "tiles", writable=False),
    ]
    remote = resolve_inputs(workflow, given(files, source="s3://b/x"), cwd=files)
    assert all(path.path != Path("s3:/b/x") for path in local_paths(workflow, remote))
    assert len(local_paths(workflow, remote)) == 2
