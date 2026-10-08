import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from context_window.config import REPO_ROOT
from context_window.settings_file import SettingsFile

SETTINGS_PATH = REPO_ROOT / "config" / "settings.toml"


@pytest.fixture(scope="module")
def repo_settings() -> SettingsFile:
    return SettingsFile.load(SETTINGS_PATH)


def test_repo_settings_file_is_valid(repo_settings: SettingsFile) -> None:
    assert len(repo_settings.interests) == 5
    beats = {source.beat for source in repo_settings.sources}
    assert beats == {"research", "releases", "industry", "community"}
    assert all(1 <= source.authority <= 5 for source in repo_settings.sources)


def test_watchlist_names_are_normalised(repo_settings: SettingsFile) -> None:
    fastmcp = next(r for r in repo_settings.watchlist if r.repo == "fastmcp")
    assert fastmcp.owner == "prefecthq"


# Real release tags seen on GitHub on 2026-10-08: (repo, tag, should it be reported?)
REAL_TAGS = [
    ("langgraph", "1.2.14", True),
    ("langgraph", "cli==0.4.33", False),
    ("langgraph", "sdk==0.4.6", False),
    ("langgraph", "checkpointpostgres==3.1.2", False),
    ("langgraph", "cli==0.4.32.dev0", False),
    ("langchain", "langchain-core==1.6.7", True),
    ("langchain", "langchain==1.4.3", True),
    ("langchain", "langchain-huggingface==1.2.3", False),
    ("langchain", "langchain-openai==1.6.7", False),
    ("pydantic-ai", "v2.54.0", True),
    ("pydantic-ai", "clai2-bleeding", False),
    ("langchain-mcp-adapters", "langchain-mcp-adapters==0.3.2", True),
    ("fastmcp", "v4.0.11", True),
    ("python-sdk", "v2.3.0", True),
    ("ollama", "v0.40.1", True),
]


@pytest.mark.parametrize(("repo", "tag", "reported"), REAL_TAGS)
def test_tag_patterns_against_real_tags(
    repo_settings: SettingsFile, repo: str, tag: str, reported: bool
) -> None:
    spec = next(r for r in repo_settings.watchlist if r.repo == repo)
    assert spec.tag_pattern is not None
    assert (re.fullmatch(spec.tag_pattern, tag) is not None) is reported


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(body, encoding="utf-8")
    return path


SOURCE = """
[[sources]]
name = "A"
beat = "industry"
kind = "rss"
url = "https://a.example/feed"
authority = 3
"""


def test_rejects_duplicate_sources(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="duplicate source url"):
        SettingsFile.load(_write(tmp_path, SOURCE + SOURCE))


def test_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SettingsFile.load(_write(tmp_path, SOURCE + 'colour = "blue"\n'))


def test_rejects_bad_authority(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SettingsFile.load(_write(tmp_path, SOURCE.replace("authority = 3", "authority = 9")))


def test_rejects_bad_regex(tmp_path: Path) -> None:
    body = '[[watchlist]]\nowner = "a"\nrepo = "b"\ntag_pattern = "v(\\\\d+"\n'
    with pytest.raises(ValidationError, match="not a valid regex"):
        SettingsFile.load(_write(tmp_path, body))


def test_rejects_duplicate_topics_case_insensitively(tmp_path: Path) -> None:
    body = (
        '[[interests]]\ntopic = "MCP"\ndescription = "x"\n'
        '[[interests]]\ntopic = "mcp"\ndescription = "y"\n'
    )
    with pytest.raises(ValidationError, match="duplicate interest topic"):
        SettingsFile.load(_write(tmp_path, body))


def test_rejects_weight_out_of_range(tmp_path: Path) -> None:
    body = '[[interests]]\ntopic = "MCP"\ndescription = "x"\nweight = 3.0\n'
    with pytest.raises(ValidationError):
        SettingsFile.load(_write(tmp_path, body))
