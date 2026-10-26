import pytest

from cif.db import repository as repo


@pytest.fixture
def conn(tmp_path):
    c = repo.connect(tmp_path / "test.db")
    yield c
    c.close()
