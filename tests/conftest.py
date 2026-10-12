import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def data_dir(tmp_path):
    """A writable copy of the mock data so tests can alter it."""
    target = tmp_path / "data"
    shutil.copytree(ROOT / "data", target)
    return target


@pytest.fixture
def out_dir(tmp_path):
    return tmp_path / "output"
