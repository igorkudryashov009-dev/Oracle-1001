"""Pytest isolation — do not inherit production API_KEYS_JSON from .env.

config_keys._load_dotenv only fills names missing from os.environ; an empty
API_KEYS_JSON blocks reload and keeps unit tests in bootstrap-open mode.
Auth-specific tests set their own keys via monkeypatch.setenv.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS_JSON", "")
    monkeypatch.delenv("API_KEYS_FILE", raising=False)
