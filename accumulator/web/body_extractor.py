# accumulator/web/body_extractor.py

import trafilatura
import hashlib


def extract_body(html: str) -> dict:
    text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        no_fallback=True
    )

    if not text:
        return {
            "paragraphs": [],
            "extracted": False,
        }

    paragraphs = [
        p.strip()
        for p in text.split("\n")
        if len(p.strip()) > 40   # length threshold avoids nav junk
    ]

    return {
        "paragraphs": paragraphs,
        "extracted": True,
    }
