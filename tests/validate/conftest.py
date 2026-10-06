from collections.abc import Callable
from pathlib import Path

import pytest

from glow.validate import ValidationReport, validate
from tests.conftest import ROOT, TOOLPACKS

FIXTURES = ROOT / "tests" / "fixtures" / "workflows"
EXAMPLES = ROOT / "examples"

Check = Callable[[str], ValidationReport]


@pytest.fixture
def check_yaml(tmp_path: Path) -> Check:
    """Validate a workflow given as YAML text against the committed toolpacks."""

    def run(text: str) -> ValidationReport:
        path = tmp_path / "workflow.yaml"
        path.write_text(text)
        return validate(path, TOOLPACKS)

    return run
