"""Shared pytest fixtures.

Every test runs with an isolated cache directory so the real
``%LOCALAPPDATA%/find-and-fix-bot`` store is never touched.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the resolution cache at a throwaway dir for the duration of a test."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    yield
