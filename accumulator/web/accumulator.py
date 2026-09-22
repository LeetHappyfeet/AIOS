from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from selenium.common.exceptions import TimeoutException

from .config import ACCUMULATOR_ID, DEFAULT_USER_AGENT, OUTPUT_DIR
from .fetcher import SeleniumFetcher
from .requests_fetcher import RequestsFetcher
from .extractor import clean_html, extract_links, extract_page_metadata
from .body_extractor import extract_body
from .queue import CrawlTask
from .writer import JSONLWriter


class WebAccumulator:
    """
    Fetch and normalize web observations without assigning truth or character
    ownership. Every emitted JSONL record is a provenance-bearing sensor record.
    """

    def __init__(self):
        self.selenium = SeleniumFetcher()
        self.requests = RequestsFetcher()
        self.writer = JSONLWriter(OUTPUT_DIR)
        self._robots: dict[str, RobotFileParser] = {}

    def _fetch(self, url: str) -> tuple[dict | None, str | None, list[dict]]:
        """Fetch with Selenium first, preserving diagnostics for every attempt."""
        attempts: list[dict] = []
        try:
            fetched = self.selenium.fetch(url)
            attempts.append({"method": "selenium", "ok": True})
            return fetched, "selenium", attempts
        except TimeoutException as exc:
            attempts.append({
                "method": "selenium",
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc) or "page load timed out",
            })
        except Exception as exc:
            attempts.append({
                "method": "selenium",
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })

        try:
            fetched = self.requests.fetch(url)
            attempts.append({
                "method": "requests",
                "ok": True,
                "status_code": fetched.get("status_code"),
                "content_type": fetched.get("content_type"),
            })
            return fetched, "requests", attempts
        except Exception as exc:
            response = getattr(exc, "response", None)
            attempts.append({
                "method": "requests",
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "status_code": getattr(response, "status_code", None),
                "content_type": (
                    response.headers.get("content-type")
                    if response is not None else None
                ),
            })
            return None, None, attempts

    @staticmethod
    def _failure_detail(reason: str, *, url: str, **details) -> dict:
        return {
            "ok": False,
            "url": url,
            "reason": reason,
            "detail": details,
            "links": [],
        }

    def _robots_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots.get(root)
        if parser is None:
            parser = RobotFileParser()
            parser.set_url(f"{root}/robots.txt")
            try:
                parser.read()
            except Exception:
                # A missing/unreachable robots file is treated as no declared
                # restriction. Crawl rate/size remains bounded independently.
                pass
            self._robots[root] = parser
        try:
            return parser.can_fetch(DEFAULT_USER_AGENT, url)
        except Exception:
            return True

    def accumulate_page(
        self,
        url: str,
        *,
        task: CrawlTask,
        parent_url: str | None = None,
        depth: int = 0,
    ) -> dict:
        if task.respect_robots and not self._robots_allowed(url):
            return self._failure_detail("robots_denied", url=url)

        fetched, fetch_method, fetch_attempts = self._fetch(url)
        if fetched is None:
            return self._failure_detail(
                "fetch_failed", url=url, fetch_attempts=fetch_attempts
            )

        html = fetched["html"]
        final_url = fetched.get("final_url") or url
        body = extract_body(html)
        if not body["extracted"]:
            fallback = clean_html(html)
            fallback["extracted"] = False
            body = fallback

        if not body.get("text"):
            return self._failure_detail(
                "empty_content",
                url=final_url,
                fetch_method=fetch_method,
                status_code=fetched.get("status_code"),
                content_type=fetched.get("content_type"),
                html_bytes=len(html.encode("utf-8")),
            )

        metadata = extract_page_metadata(html, final_url)
        content_sha = body.get("text_sha256") or hashlib.sha256(
            body["text"].encode("utf-8")
        ).hexdigest()
        retrieved_at = datetime.now(timezone.utc).isoformat()

        record = {
            "schema_version": "accumulator.web.v2",
            "accumulator_id": ACCUMULATOR_ID,
            "source_type": "web_page",
            "source": {
                "source_id": task.source_id,
                "source_kind": task.source_kind,
                "source_name": task.source_name,
                "speaker_id": task.speaker_id,
            },
            "target": {
                "character_id": task.target_character_id,
                "world_id": task.target_world_id,
            },
            "ingestion": {
                "mode": task.ingest_mode,
                "consumption_mode": task.consumption_mode,
                "corpus_profile_key": task.corpus_profile_key,
            },
            "crawl": {
                "task_id": task.task_id,
                "mode": task.crawl_mode,
                "seed_url": task.url,
                "parent_url": parent_url,
                "depth": depth,
            },
            "url": final_url,
            "requested_url": url,
            "retrieved_at": retrieved_at,
            "fetch": {
                "method": fetch_method,
                "rendered": fetched.get("rendered", False),
                "user_agent": fetched.get("user_agent"),
                "load_time_ms": fetched.get("load_time_ms"),
                "status_code": fetched.get("status_code"),
                "content_type": fetched.get("content_type"),
            },
            "document": metadata,
            "content": {
                "lang": "en",
                "title": metadata.get("title"),
                "text": body["text"],
            },
            "structure": {
                "paragraph_count": body["paragraph_count"],
                "approx_tokens": body["approx_tokens"],
            },
            "hints": {
                "asserted_facts": False,
                "body_extracted": body.get("extracted", False),
                "target_character_is_hint": task.target_character_id is not None,
                "target_world_is_hint": task.target_world_id is not None,
                "cold_corpus": task.ingest_mode in {"corpus", "consume"},
                "intentional_consumption": task.ingest_mode == "consume",
            },
            "raw": {
                "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
                "text_sha256": content_sha,
            },
        }

        output_path = self.writer.write(record)
        links = extract_links(html, final_url)
        return {
            "ok": True,
            "url": final_url,
            "record": record,
            "output_path": str(output_path),
            "links": links,
        }

    def accumulate_task(self, task: CrawlTask, *, on_progress=None) -> dict:
        """Accumulate one page or a bounded same-site crawl."""
        max_pages = max(1, min(int(task.max_pages), 1000))
        max_depth = max(0, min(int(task.max_depth), 10))
        if task.crawl_mode == "page":
            max_pages = 1
            max_depth = 0

        seed_host = urlparse(task.url).netloc.lower()
        frontier = deque([(task.url, None, 0)])
        queued = {task.url}
        visited: set[str] = set()
        written = 0
        failed = 0
        failures: list[dict] = []

        while frontier and len(visited) < max_pages:
            url, parent_url, depth = frontier.popleft()
            if url in visited:
                continue
            visited.add(url)

            if on_progress:
                on_progress(
                    current_url=url,
                    pages_discovered=len(queued),
                    pages_written=written,
                    pages_failed=failed,
                )

            # Source-bound crawls never fetch an off-domain frontier entry.
            # This also protects recovered legacy tasks whose queue already escaped.
            if task.same_domain_only and urlparse(url).netloc.lower() != seed_host:
                failed += 1
                failures.append({
                    "url": url,
                    "reason": "off_domain_blocked",
                    "detail": {"seed_host": seed_host},
                })
                continue

            result = self.accumulate_page(
                url,
                task=task,
                parent_url=parent_url,
                depth=depth,
            )
            if result["ok"]:
                written += 1
            else:
                failed += 1
                failures.append({
                    "url": result.get("url") or url,
                    "reason": result.get("reason") or "unknown",
                    "detail": result.get("detail") or {},
                })

            if (
                task.crawl_mode != "site"
                or not result["ok"]
                or depth >= max_depth
            ):
                continue

            for link in result.get("links", []):
                if link in queued or link in visited:
                    continue
                if task.same_domain_only and urlparse(link).netloc.lower() != seed_host:
                    continue
                queued.add(link)
                frontier.append((link, result["url"], depth + 1))
                if len(queued) >= max_pages:
                    break

        if on_progress:
            on_progress(
                current_url=None,
                pages_discovered=len(queued),
                pages_written=written,
                pages_failed=failed,
            )

        return {
            "task_id": task.task_id,
            "pages_discovered": len(queued),
            "pages_visited": len(visited),
            "pages_written": written,
            "pages_failed": failed,
            "failures": failures,
        }

    def shutdown(self):
        self.selenium.close()
