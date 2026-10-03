"""Compiler output is pinned by golden files. Refresh them with UPDATE_GOLDEN=1."""

import os

import pytest

from glow.compile import compile_workflow, to_yaml
from tests.compile.conftest import COMPILED, GOLDEN, OPTIONS, load_ir


@pytest.mark.parametrize("name", sorted(COMPILED))
def test_output_matches_golden_file(name: str) -> None:
    text = to_yaml(compile_workflow(load_ir(COMPILED[name]), OPTIONS))
    golden = GOLDEN / f"{name}.yaml"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden.write_text(text)
    assert golden.exists(), f"{golden} is missing; run with UPDATE_GOLDEN=1"
    assert text == golden.read_text(), f"{name} changed; rerun with UPDATE_GOLDEN=1 and review"


def test_every_golden_file_has_a_source() -> None:
    assert {path.stem for path in GOLDEN.glob("*.yaml")} == set(COMPILED)
