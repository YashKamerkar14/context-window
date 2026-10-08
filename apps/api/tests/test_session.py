import pytest

from context_window.db.session import is_pooled, make_engine, sqlalchemy_url, validate_schema_name


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@h/db?sslmode=require", "postgresql+psycopg://u:p@h/db?sslmode=require"),
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ],
)
def test_sqlalchemy_url(url: str, expected: str) -> None:
    assert sqlalchemy_url(url) == expected


@pytest.mark.parametrize("url", ["mysql://u:p@h/db", "no-scheme"])
def test_sqlalchemy_url_rejects_others(url: str) -> None:
    with pytest.raises(ValueError):
        sqlalchemy_url(url)


def test_is_pooled() -> None:
    assert is_pooled("postgresql://u:p@ep-x-pooler.us-east-1.aws.neon.tech/db")
    assert not is_pooled("postgresql://u:p@ep-x.us-east-1.aws.neon.tech/db")


@pytest.mark.parametrize("name", ["test_abc123", "_x", "a"])
def test_valid_schema_names(name: str) -> None:
    assert validate_schema_name(name) == name


@pytest.mark.parametrize("name", ["Test", "1abc", 'x"; DROP TABLE runs; --', "", "a" * 64])
def test_invalid_schema_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_schema_name(name)


def test_search_path_needs_direct_url() -> None:
    with pytest.raises(ValueError, match="direct"):
        make_engine("postgresql://u:p@ep-x-pooler.neon.tech/db", search_path="test_x")
