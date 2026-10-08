import pytest

from context_window.memory.canonical import (
    arxiv_key,
    canonical_key,
    canonicalize_url,
    github_release_key,
    normalize_pypi_name,
    pypi_release_key,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # scheme, host case, www, default port, trailing slash, fragment
        ("http://Example.com/Post/", "https://example.com/Post"),
        ("https://www.example.com:443/a/b/#section", "https://example.com/a/b"),
        ("http://example.com:80", "https://example.com/"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("https://example.com./x", "https://example.com/x"),
        # tracking parameters are removed, the rest are sorted
        (
            "https://example.com/p?utm_source=x&b=2&a=1&utm_MEDIUM=y",
            "https://example.com/p?a=1&b=2",
        ),
        ("https://example.com/p?fbclid=abc&gclid=def&ref=hn", "https://example.com/p"),
        ("https://example.com/p?q=a+b", "https://example.com/p?q=a%20b"),
        ("https://example.com/p?empty=", "https://example.com/p?empty="),
        # credentials never survive
        ("https://user:secret@example.com/p", "https://example.com/p"),
        ("  https://example.com/p  ", "https://example.com/p"),
    ],
)
def test_canonicalize_url(url: str, expected: str) -> None:
    assert canonicalize_url(url) == expected


@pytest.mark.parametrize(
    "url", ["ftp://example.com/x", "mailto:a@b.c", "example.com/x", "https://"]
)
def test_canonicalize_rejects_non_http(url: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_url(url)


def test_canonicalize_is_idempotent() -> None:
    url = "http://www.Example.com/a/?utm_source=x&z=1&a=2#top"
    once = canonicalize_url(url)
    assert canonicalize_url(once) == once


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/abs/2610.03799",
        "https://arxiv.org/abs/2610.03799v2",
        "http://arxiv.org/pdf/2610.03799v1",
        "https://arxiv.org/pdf/2610.03799v3.pdf",
        "https://arxiv.org/html/2610.03799v1",
        "https://export.arxiv.org/abs/2610.03799",
        "https://www.arxiv.org/abs/2610.03799?context=cs.CL",
        "https://huggingface.co/papers/2610.03799",
    ],
)
def test_every_arxiv_form_maps_to_one_key(url: str) -> None:
    assert canonical_key(url) == "arxiv:2610.03799"


def test_old_style_arxiv_ids() -> None:
    assert canonical_key("https://arxiv.org/abs/hep-th/9901001v2") == "arxiv:hep-th/9901001"
    assert canonical_key("https://arxiv.org/abs/math.GT/0309136") == "arxiv:math.GT/0309136"


def test_four_digit_arxiv_ids() -> None:
    assert arxiv_key("0704.0001v1") == "arxiv:0704.0001"


def test_github_release_url() -> None:
    url = "https://github.com/LangChain-AI/LangGraph/releases/tag/1.2.14"
    assert canonical_key(url) == "gh:langchain-ai/langgraph@1.2.14"
    assert canonical_key(url) == github_release_key("langchain-ai", "langgraph", "1.2.14")


def test_github_release_tag_with_slash_and_equals() -> None:
    url = "https://github.com/langchain-ai/langchain/releases/tag/langchain-core%3D%3D1.6.7"
    assert canonical_key(url) == "gh:langchain-ai/langchain@langchain-core==1.6.7"


def test_pypi_release_url() -> None:
    assert (
        canonical_key("https://pypi.org/project/LangChain_Core/1.6.7/")
        == "pypi:langchain-core@1.6.7"
    )
    assert pypi_release_key("Foo.Bar", "2.0") == "pypi:foo-bar@2.0"


def test_pypi_project_page_without_version_is_just_a_url() -> None:
    assert (
        canonical_key("https://pypi.org/project/langgraph/") == "https://pypi.org/project/langgraph"
    )


def test_hacker_news_thread() -> None:
    assert canonical_key("https://news.ycombinator.com/item?id=41234567") == "hn:41234567"
    assert canonical_key("https://news.ycombinator.com/item?id=abc").startswith("https://")


def test_other_urls_use_the_canonical_url() -> None:
    assert (
        canonical_key("http://www.openai.com/news/x/?utm_source=rss") == "https://openai.com/news/x"
    )


@pytest.mark.parametrize(("name", "expected"), [("Foo_Bar", "foo-bar"), ("a.b-c__d", "a-b-c-d")])
def test_normalize_pypi_name(name: str, expected: str) -> None:
    assert normalize_pypi_name(name) == expected


def test_arxiv_key_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        arxiv_key("not-an-id")
