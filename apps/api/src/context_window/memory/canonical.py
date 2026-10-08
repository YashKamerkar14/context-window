"""Canonical URLs and keys, so the same thing fetched twice is recognised as one item.

- `canonicalize_url` normalises a URL: https, lower-case host without `www.`, no default
  port, no tracking parameters, sorted query, no fragment, no trailing slash.
- `canonical_key` goes further for sources with stable identifiers: every arXiv URL form
  (abs, pdf, html, any version, Hugging Face papers page) maps to `arxiv:<id>`; a GitHub
  release to `gh:<owner>/<repo>@<tag>`; a PyPI release to `pypi:<package>@<version>`;
  a Hacker News thread to `hn:<id>`. Anything else uses its canonical URL.
"""

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "_hsenc",
        "_hsmi",
        "mkt_tok",
        "ref",
        "ref_src",
        "ref_url",
    }
)
TRACKING_PREFIXES = ("utm_",)

# New-style arXiv ids (2007+): YYMM.NNNN or YYMM.NNNNN. Old-style: archive(.SUBJ)/YYMMNNN.
_ARXIV_NEW = r"\d{4}\.\d{4,5}"
_ARXIV_OLD = r"[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7}"
_ARXIV_ID = re.compile(rf"^(?P<id>{_ARXIV_NEW}|{_ARXIV_OLD})(?:v\d+)?(?:\.pdf)?$")
_ARXIV_HOSTS = frozenset({"arxiv.org", "export.arxiv.org"})


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def _host(netloc_host: str) -> str:
    host = netloc_host.lower().rstrip(".")
    return host.removeprefix("www.")


def canonicalize_url(url: str) -> str:
    """Normalise an http(s) URL. Raises ValueError for other schemes or a missing host."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(f"not an http(s) URL: {url!r}")
    if not parts.hostname:
        raise ValueError(f"URL has no host: {url!r}")

    host = _host(parts.hostname)
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    query_pairs = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(name)
    ]
    query = urlencode(sorted(query_pairs), quote_via=quote)
    return urlunsplit(("https", netloc, path, query, ""))


def normalize_pypi_name(name: str) -> str:
    """PEP 503 normalisation: `Foo_Bar.baz` and `foo-bar-baz` are the same project."""
    return re.sub(r"[-_.]+", "-", name).lower()


def arxiv_key(arxiv_id: str) -> str:
    match = _ARXIV_ID.match(arxiv_id.strip())
    if match is None:
        raise ValueError(f"not an arXiv id: {arxiv_id!r}")
    return f"arxiv:{match.group('id')}"


def github_release_key(owner: str, repo: str, tag: str) -> str:
    # GitHub owner and repo names are case-insensitive; tags are not.
    return f"gh:{owner.lower()}/{repo.lower()}@{tag}"


def pypi_release_key(package: str, version: str) -> str:
    return f"pypi:{normalize_pypi_name(package)}@{version}"


def _path_segments(path: str) -> list[str]:
    return [unquote(segment) for segment in path.split("/") if segment]


def _arxiv_from_path(segments: list[str]) -> str | None:
    if len(segments) >= 2 and segments[0] in ("abs", "pdf", "html"):
        try:
            return arxiv_key("/".join(segments[1:]))
        except ValueError:
            return None
    return None


def canonical_key(url: str) -> str:
    """A stable identity for the thing a URL points at."""
    canonical = canonicalize_url(url)
    parts = urlsplit(canonical)
    host = parts.hostname or ""
    segments = _path_segments(parts.path)

    if host in _ARXIV_HOSTS and (key := _arxiv_from_path(segments)) is not None:
        return key

    if host == "huggingface.co" and len(segments) == 2 and segments[0] == "papers":
        try:
            return arxiv_key(segments[1])
        except ValueError:
            pass

    if host == "github.com" and len(segments) >= 5 and segments[2:4] == ["releases", "tag"]:
        return github_release_key(segments[0], segments[1], "/".join(segments[4:]))

    if host == "pypi.org" and len(segments) == 3 and segments[0] == "project":
        return pypi_release_key(segments[1], segments[2])

    if host == "news.ycombinator.com" and segments == ["item"]:
        item_id = dict(parse_qsl(parts.query)).get("id", "")
        if item_id.isdigit():
            return f"hn:{item_id}"

    return canonical
