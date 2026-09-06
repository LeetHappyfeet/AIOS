# accumulator/web/requests_fetcher.py

import time
import requests

from .config import DEFAULT_USER_AGENT


class RequestsFetcher:
    def fetch(self, url: str) -> dict:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
        }

        start = time.time()
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        load_time_ms = int((time.time() - start) * 1000)

        return {
            "html": resp.text,
            "final_url": resp.url,
            "load_time_ms": load_time_ms,
            "rendered": False,
            "user_agent": DEFAULT_USER_AGENT,
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type"),
        }
