# accumulator/web/extractor.py

from bs4 import BeautifulSoup
import hashlib


CONTENT_TAGS = ["article", "main", "section"]


def clean_html(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    # Remove garbage
    for tag in soup(["script", "style", "nav", "footer", "aside", "noscript", "form"]):
        tag.decompose()

    # Prefer semantic containers if present
    candidates = []
    for tag in CONTENT_TAGS:
        candidates.extend(soup.find_all(tag))

    if candidates:
        # pick the largest text block
        container = max(
            candidates,
            key=lambda el: len(el.get_text(strip=True))
        )
        text = container.get_text(separator="\n")
    else:
        text = soup.get_text(separator="\n")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    clean_text = "\n".join(lines)

    return {
        "text": clean_text,
        "text_sha256": hashlib.sha256(clean_text.encode("utf-8")).hexdigest(),
        "paragraph_count": len([l for l in lines if len(l) > 80]),
        "approx_tokens": int(len(clean_text) / 4),
    }
