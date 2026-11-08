import os
import tempfile
from pathlib import Path

# Use a throwaway database for the whole test run (set before the app is imported).
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["MY_USERNAMES"] = "Player123"  # fixtures use this name

import pytest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_text():
    return lambda name: (FIXTURES / name).read_text()
