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
from .extractor import clean_html, extract_links, extract_page_metadata, extract_structured_source_metadata
from .body_extractor import extract_body
from .queue import CrawlTask
from .crawl_policy import evaluate_page, evaluate_url, normalize_url
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
        """Fetch with requests first, falling back to Selenium when needed."""
        attempts: list[dict] = []
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

        # Never silently admit a redirect outside the crawl boundary.
        if task.same_domain_only:
            seed_host = urlparse(task.url).netloc.lower()
            final_host = urlparse(final_url).netloc.lower()
            if final_host != seed_host:
                return self._failure_detail(
                    "redirect_off_domain",
                    url=final_url,
                    requested_url=url,
                    seed_host=seed_host,
                    final_host=final_host,
                    fetch_method=fetch_method,
                    fetch_attempts=fetch_attempts,
                )

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
        structured_metadata = extract_structured_source_metadata(html, final_url)

        # AO3 chapter pages do not reliably repeat the work-level fandom tags.
        # Fetch the canonical work page only for repository-native metadata;
        # chapter text/provenance remains attached to the requested chapter URL.
        parsed_final = urlparse(final_url)
        if (parsed_final.hostname or "").lower() in {"archiveofourown.org", "www.archiveofourown.org"}:
            parts = [part for part in parsed_final.path.split("/") if part]
            if len(parts) >= 4 and parts[0] == "works" and parts[2] == "chapters":
                work_url = f"{parsed_final.scheme or 'https'}://{parsed_final.netloc}/works/{parts[1]}"
                work_fetched, _, _ = self._fetch(work_url)
                if work_fetched is not None:
                    work_html = work_fetched.get("html") or ""
                    work_meta = extract_structured_source_metadata(work_html, work_url)
                    if isinstance(work_meta.get("ao3"), dict):
                        structured_metadata = dict(structured_metadata or {})
                        structured_metadata["ao3"] = work_meta["ao3"]
                        structured_metadata["ao3_work_url"] = work_url

        page_decision = evaluate_page(
            metadata=metadata,
            body=body,
            html=html,
        )
        if not page_decision.accept:
            return self._failure_detail(
                "page_rejected",
                url=final_url,
                rejection=page_decision.reason,
                fetch_method=fetch_method,
                status_code=fetched.get("status_code"),
                content_type=fetched.get("content_type"),
                fetch_attempts=fetch_attempts,
                title=metadata.get("title"),
                text_chars=len(body.get("text") or ""),
                text_preview=(body.get("text") or "")[:500],
            )

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
                "knowledge_domain": task.knowledge_domain,
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
            "structured_metadata": structured_metadata,
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

        seed_url = normalize_url(task.url)
        seed_host = urlparse(seed_url).netloc.lower()
        frontier = deque([(seed_url, None, 0)])
        queued = {seed_url}
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

            url_decision = evaluate_url(url, seed=(depth == 0))
            if not url_decision.accept:
                failed += 1
                failures.append({
                    "url": url,
                    "reason": "url_rejected",
                    "detail": {"rejection": url_decision.reason},
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
                link = normalize_url(link)
                if link in queued or link in visited:
                    continue
                if task.same_domain_only and urlparse(link).netloc.lower() != seed_host:
                    continue
                link_decision = evaluate_url(link)
                if not link_decision.accept:
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
