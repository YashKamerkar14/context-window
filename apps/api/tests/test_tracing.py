import os

import pytest

from context_window.config import Settings
from context_window.tracing import configure_tracing


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # monkeypatch restores these after each test, so tracing never leaks into other tests.
    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
        monkeypatch.delenv(name, raising=False)


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


def test_tracing_needs_a_key() -> None:
    assert configure_tracing(_settings()) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"


def test_tracing_exports_the_settings() -> None:
    settings = _settings(langsmith_api_key="lsv2_pt_test", langsmith_project="cw-test")
    assert configure_tracing(settings) is True
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_pt_test"
    assert os.environ["LANGSMITH_PROJECT"] == "cw-test"


def test_tracing_can_be_switched_off() -> None:
    settings = _settings(langsmith_api_key="lsv2_pt_test", langsmith_tracing=False)
    assert configure_tracing(settings) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"
