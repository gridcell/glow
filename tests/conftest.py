import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLPACKS = ROOT / "toolpacks"


@pytest.fixture
def toolpacks(tmp_path: Path) -> Path:
    """A writable copy of the committed toolpacks directory, lock included."""
    return Path(shutil.copytree(TOOLPACKS, tmp_path / "toolpacks"))
