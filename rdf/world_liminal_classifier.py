#\aios_app\rdf\world_liminal_classifier.py
# -------------------------------------------------
# Classification rules (modular + ordered)
# -------------------------------------------------

from typing import Optional, Callable, NamedTuple
import re

class ClassificationRule(NamedTuple):
    name: str
    priority: int
    fn: Callable[[dict], Optional[str]]


PRONOUNS = {
    "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them",
}

INTERROGATIVES = {
    "what", "which", "who", "where", "when", "how", "why"
}

NAV_KEYWORDS = {
    "click", "subscribe", "watch", "listen", "log",
    "menu", "sign", "account", "profile",
}

UI_NOUNS = {
    "video", "videos", "quote", "quotes",
    "story", "stories", "article", "articles",
    "headline", "headlines",
}

FOOTER_PATTERNS = [
    r"all rights reserved",
    r"terms of use",
    r"privacy policy",
    r"©",
    r"copyright",
]


# -------------------------------------------------
# Individual rule functions
# -------------------------------------------------

def rule_pronoun_heavy(row: dict) -> Optional[str]:
    subj = (row.get("subject") or "").strip().lower()
    if subj in PRONOUNS:
        return "pronoun-heavy"
    return None


def rule_interrogative(row: dict) -> Optional[str]:
    subj = (row.get("subject") or "").strip().lower()
    if subj in INTERROGATIVES:
        return "navigation"
    return None


def rule_ui_noun(row: dict) -> Optional[str]:
    subj = (row.get("subject") or "").strip().lower()
    if subj in UI_NOUNS:
        return "navigation"
    return None


def rule_navigation_keyword(row: dict) -> Optional[str]:
    subj = (row.get("subject") or "").strip().lower()
    pred = (row.get("predicate") or "").strip().lower()
    if subj in NAV_KEYWORDS or pred in NAV_KEYWORDS:
        return "navigation"
    return None


def rule_footer(row: dict) -> Optional[str]:
    text = (row.get("raw_text") or "").lower()
    subj = (row.get("subject") or "").lower()
    pred = (row.get("predicate") or "").lower()

    for pat in FOOTER_PATTERNS:
        if (
            re.search(pat, text)
            or re.search(pat, subj)
            or re.search(pat, pred)
        ):
            return "footer"

    return None


def rule_reference(row: dict) -> Optional[str]:
    """
    Bibliographic / citation-like material:
    - ISBNs
    - page numbers
    - dates
    - retrieval notes
    - bare domains / publishers
    """
    text = (row.get("raw_text") or "").strip()

    if not text:
        return None

    lower = text.lower()

    # ISBN / ISSN
    if re.search(r"\bISBN\b|\bISSN\b", text, re.IGNORECASE):
        return "reference"

    # Page numbers (e.g. "p. 140.")
    if re.fullmatch(r"\s*p\.\s*\d+\s*\.?\s*", lower):
        return "reference"

    # Retrieval / archive notes
    if lower.startswith("retrieved ") or "archived from the original" in lower:
        return "reference"

    # Standalone dates (e.g. "August 22, 1990")
    if re.fullmatch(
        r"\s*(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\s*",
        text,
    ):
        return "reference"

    # Year-only lines
    if re.fullmatch(r"\s*\d{4}\s*", lower):
        return "reference"

    # Bare domains (DVDizzy.com, People.)
    if re.fullmatch(r"[A-Za-z0-9\-]+\.(com|org|net|edu)\.?", lower):
        return "reference"

    return None


# -------------------------------------------------
# Rule registry (explicit order)
# -------------------------------------------------

RULES = sorted(
    [
        ClassificationRule("pronoun-heavy", 10, rule_pronoun_heavy),
        ClassificationRule("interrogative", 20, rule_interrogative),
        ClassificationRule("ui-noun", 30, rule_ui_noun),
        ClassificationRule("navigation-keyword", 40, rule_navigation_keyword),
        ClassificationRule("footer", 50, rule_footer),
        ClassificationRule("reference", 60, rule_reference),
    ],
    key=lambda r: r.priority,
)


# -------------------------------------------------
# Classifier entrypoint
# -------------------------------------------------

def classify_claim(row: dict) -> str:
    """
    Determine world:contentKind using ordered deterministic rules.

    - First matching rule wins
    - Default: 'content'
    - No mutation, no truth assertion
    """
    for rule in RULES:
        result = rule.fn(row)
        if result is not None:
            return result

    return "content"
