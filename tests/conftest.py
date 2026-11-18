from pathlib import Path
import shutil
import tempfile
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def short_dir():
    path = Path(tempfile.mkdtemp(prefix="rrg-", dir="/tmp"))
    yield path
    shutil.rmtree(path)


@pytest.fixture(scope="session", autouse=True)
def warm_dependencies():
    import cv2
    import numpy

    assert cv2 and numpy
