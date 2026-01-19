# accumulator/web/body_extractor.py

import hashlib
import re
import trafilatura


MAX_PARAGRAPH_CHARS = 1200
MIN_PARAGRAPH_CHARS = 200


def extract_body(html: str) -> dict:
    """
    Extract article body text and reconstruct paragraphs heuristically.

    Strategy:
    1. Prefer double-newline paragraphs if present
    2. Otherwise, rebuild paragraphs from single-line sentence output
    """

    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        no_fallback=True,
    )

    if not text:
        return {
            "text": "",
            "text_sha256": None,
            "paragraph_count": 0,
            "approx_tokens": 0,
            "extracted": False,
        }

    # Normalize newlines
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # -------------------------------------------------
    # Case 1: Real paragraph breaks exist
    # -------------------------------------------------
    if "\n\n" in text:
        raw_paragraphs = re.split(r"\n\s*\n+", text)
        paragraphs = [
            p.strip()
            for p in raw_paragraphs
            if len(p.strip()) >= MIN_PARAGRAPH_CHARS
        ]

    # -------------------------------------------------
    # Case 2: Sentence-per-line output (Trafilatura default)
    # -------------------------------------------------
    else:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        paragraphs = []

        buffer = ""

        for line in lines:
            if buffer:
                buffer += " " + line
            else:
                buffer = line

            if (
                len(buffer) >= MIN_PARAGRAPH_CHARS
                and buffer.rstrip().endswith((".", "?", "!"))
            ) or len(buffer) >= MAX_PARAGRAPH_CHARS:
                paragraphs.append(buffer.strip())
                buffer = ""

        if buffer:
            paragraphs.append(buffer.strip())

    joined = "\n\n".join(paragraphs)

    return {
        "text": joined,
        "text_sha256": hashlib.sha256(joined.encode("utf-8")).hexdigest(),
        "paragraph_count": len(paragraphs),
        "approx_tokens": int(len(joined) / 4),
        "extracted": True,
    }
