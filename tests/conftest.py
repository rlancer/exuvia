import pytest


@pytest.fixture(autouse=True)
def _no_fossil_mirror(monkeypatch):
    """The Fossil mirror (exuvia/fossil.py) turns itself on wherever fossil is
    installed. Keep it out of tests that are not about it; test_fossil.py
    turns it back on."""
    monkeypatch.setenv("EXUVIA_FOSSIL", "off")
