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
    # Run the scan in-thread by default so the suite stays fast and doesn't
    # spawn a subprocess per app instance. The dedicated process-offload path
    # is exercised explicitly by its own test.
    monkeypatch.setenv("FINDFIX_SCAN_IN_PROCESS", "0")
    yield
