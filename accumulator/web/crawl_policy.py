from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
}
BLOCKED_QUERY_KEYS = {
    "action", "diff", "oldid", "printable", "veaction",
}
BLOCKED_PATH_PARTS = {
    "login", "logout", "register", "signup", "signin", "search",
}
BLOCKED_WIKI_NAMESPACES = {
    "blog", "card", "category", "fan", "file", "forum", "help",
    "media", "mediawiki", "special", "talk", "template", "user",
}
CHALLENGE_TITLES = {
    "just a moment", "attention required", "access denied",
    "checking your browser", "security check",
}
CHALLENGE_MARKERS = (
    "enable javascript and cookies to continue",
    "checking if the site connection is secure",
    "verify you are human",
)


@dataclass(frozen=True)
class AdmissionDecision:
    accept: bool
    reason: str | None = None


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in TRACKING_QUERY_KEYS:
            continue
        query.append((key, value))
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path or "/",
        "",
        urlencode(query, doseq=True),
        "",
    ))


def evaluate_url(url: str, *, seed: bool = False) -> AdmissionDecision:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return AdmissionDecision(False, "unsupported_url")

    path = parsed.path or "/"
    lower_path = path.lower()
    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}

    if query_keys & BLOCKED_QUERY_KEYS:
        return AdmissionDecision(False, "action_url")

    segments = [segment for segment in lower_path.split("/") if segment]
    if any(segment in BLOCKED_PATH_PARTS for segment in segments):
        return AdmissionDecision(False, "administrative_path")

    # MediaWiki/Fandom content pages commonly live below /wiki/. Namespace
    # pages are navigation/administration surfaces, not corpus documents.
    match = re.search(r"/wiki/([^/?#]+)", path, flags=re.IGNORECASE)
    if match:
        article = match.group(1)
        if ":" in article:
            namespace = article.split(":", 1)[0].lower()
            if namespace in BLOCKED_WIKI_NAMESPACES:
                return AdmissionDecision(False, "wiki_namespace")

    return AdmissionDecision(True)


def evaluate_page(*, metadata: dict, body: dict, html: str) -> AdmissionDecision:
    title = str(metadata.get("title") or "").strip().lower().rstrip(".")
    text = str(body.get("text") or "").strip()
    lower_text = text.lower()

    # Challenge infrastructure can be present in the HTML of perfectly valid
    # pages (notably Fandom/Cloudflare). Only classify a challenge when the
    # rendered document itself presents challenge evidence to the reader.
    challenge_title = title in CHALLENGE_TITLES
    # Body markers are only meaningful when the extracted page is itself a
    # small interstitial. Large Fandom articles can contain challenge/security
    # boilerplate in visible footer or platform text.
    challenge_body = (
        len(text) < 1500
        and any(marker in lower_text for marker in CHALLENGE_MARKERS)
    )
    if challenge_title or challenge_body:
        return AdmissionDecision(False, "challenge_page")

    if not text:
        return AdmissionDecision(False, "empty_content")

    # Avoid admitting tiny shells produced by login/error/navigation pages.
    # Seeds get no special exemption here: a requested garbage page should not
    # become corpus evidence merely because it was entered manually.
    if len(text) < 120:
        return AdmissionDecision(False, "low_content")

    return AdmissionDecision(True)
