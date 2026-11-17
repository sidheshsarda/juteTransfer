"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _open_access(monkeypatch):
    """The flows under test are not about who is logged in: run them with the
    admin guard permissive (tests/test_access.py switches it back on)."""
    monkeypatch.setenv("JT_LEGACY_LOGIN", "1")
