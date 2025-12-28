# accumulator/web/accumulator.py

from datetime import datetime
import hashlib
import traceback

from selenium.common.exceptions import TimeoutException

from .config import ACCUMULATOR_ID, OUTPUT_DIR
from .fetcher import SeleniumFetcher
from .requests_fetcher import RequestsFetcher
from .extractor import clean_html
from .body_extractor import extract_body
from .writer import JSONLWriter


class WebAccumulator:
    """
    WebAccumulator
    ----------------
    Responsible for:
    - Fetching a web page (Selenium with Requests fallback)
    - Extracting main body text (trafilatura)
    - Normalizing text for downstream ingestion
    - Emitting immutable JSONL records

    This class MUST NOT:
    - Infer truth
    - Modify semantics
    - Perform ontology extraction
    """

    def __init__(self):
        self.selenium = SeleniumFetcher()
        self.requests = RequestsFetcher()
        self.writer = JSONLWriter(OUTPUT_DIR)

    def accumulate(self, url: str) -> bool:
        """
        Attempt to fetch, normalize, and store a web page.

        Returns:
            True  -> record written
            False -> fetch failed, nothing written
        """

        fetched = None
        fetch_method = None

        # -------------------------------------------------
        # 1. Attempt Selenium fetch (JS-heavy sites)
        # -------------------------------------------------
        try:
            fetched = self.selenium.fetch(url)
            fetch_method = "selenium"
        except TimeoutException:
            # Selenium renderer stalled – expected on some sites
            pass
        except Exception:
            # Unexpected Selenium failure – log and continue
            traceback.print_exc()

        # -------------------------------------------------
        # 2. Fallback to Requests (static / wiki sites)
        # -------------------------------------------------
        if fetched is None:
            try:
                fetched = self.requests.fetch(url)
                fetch_method = "requests"
            except Exception:
                # Requests can also legitimately fail (slow sites, TLS quirks)
                traceback.print_exc()
                return False

        html = fetched["html"]

        # -------------------------------------------------
        # 3. Extract main body text (preferred)
        # -------------------------------------------------
        body = extract_body(html)

        # -------------------------------------------------
        # 4. Fallback: naive visible-text extraction
        # -------------------------------------------------
        if not body.get("extracted"):
            body = clean_html(html)
            body["extracted"] = False

        # -------------------------------------------------
        # 5. Emit immutable accumulator record
        # -------------------------------------------------
        record = {
            "schema_version": "accumulator.web.v1",
            "accumulator_id": ACCUMULATOR_ID,
            "source_type": "web_page",
            "url": url,
            "retrieved_at": datetime.utcnow().isoformat() + "Z",

            "fetch": {
                "method": fetch_method,
                "rendered": fetched.get("rendered", False),
                "user_agent": fetched.get("user_agent"),
                "load_time_ms": fetched.get("load_time_ms"),
            },

            "content": {
                "lang": "en",
                "text": body["text"],
            },

            "structure": {
                "paragraph_count": body["paragraph_count"],
                "approx_tokens": body["approx_tokens"],
            },

            "hints": {
                "asserted_facts": False,
                "body_extracted": body.get("extracted", False),
            },

            "raw": {
                "html_sha256": hashlib.sha256(
                    html.encode("utf-8")
                ).hexdigest(),
                "text_sha256": body.get("text_sha256"),
            },
        }

        self.writer.write(record)
        return True

    def shutdown(self):
        """Clean shutdown for worker threads."""
        self.selenium.close()
