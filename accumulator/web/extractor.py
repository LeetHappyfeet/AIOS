from __future__ import annotations

from bs4 import BeautifulSoup
import hashlib
from urllib.parse import urljoin, urlparse


CONTENT_TAGS = ["article", "main", "section"]


def _meta_content(soup: BeautifulSoup, *keys: tuple[str, str]) -> str | None:
    for attr, value in keys:
        tag = soup.find("meta", attrs={attr: value})
        if tag and tag.get("content"):
            content = str(tag.get("content")).strip()
            if content:
                return content
    return None


def extract_page_metadata(html: str, url: str) -> dict:
    """Extract provenance-bearing page metadata without semantic inference."""
    soup = BeautifulSoup(html, "lxml")

    title = (
        _meta_content(
            soup,
            ("property", "og:title"),
            ("name", "twitter:title"),
        )
        or (soup.title.string.strip() if soup.title and soup.title.string else None)
    )

    author = _meta_content(
        soup,
        ("name", "author"),
        ("property", "article:author"),
    )
    published_at = _meta_content(
        soup,
        ("property", "article:published_time"),
        ("name", "date"),
        ("name", "datePublished"),
    )
    updated_at = _meta_content(
        soup,
        ("property", "article:modified_time"),
        ("name", "last-modified"),
        ("name", "dateModified"),
    )
    site_name = _meta_content(soup, ("property", "og:site_name"))

    canonical = None
    canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical_tag and canonical_tag.get("href"):
        canonical = urljoin(url, str(canonical_tag.get("href")).strip())

    return {
        "title": title,
        "author": author,
        "published_at": published_at,
        "updated_at": updated_at,
        "site_name": site_name,
        "canonical_url": canonical,
    }


def extract_links(html: str, base_url: str) -> list[str]:
    """Return normalized HTTP(S) links for bounded crawl discovery."""
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    links: list[str] = []

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        normalized = parsed._replace(fragment="").geturl()
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)

    return links


def clean_html(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "nav", "footer", "aside", "noscript", "form"]):
        tag.decompose()

    candidates = []
    for tag in CONTENT_TAGS:
        candidates.extend(soup.find_all(tag))

    if candidates:
        container = max(candidates, key=lambda el: len(el.get_text(strip=True)))
        text = container.get_text(separator="\n")
    else:
        text = soup.get_text(separator="\n")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    clean_text = "\n".join(lines)

    return {
        "text": clean_text,
        "text_sha256": hashlib.sha256(clean_text.encode("utf-8")).hexdigest(),
        "paragraph_count": len([line for line in lines if len(line) > 80]),
        "approx_tokens": int(len(clean_text) / 4),
    }
