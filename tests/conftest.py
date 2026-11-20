import logging
from pathlib import Path

import pytest

from gbwatch.redact import install_redaction

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _redact_logs():
    install_redaction()
    yield
