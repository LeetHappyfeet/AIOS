# aios/rdf/fuseki.py

import logging
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class FusekiError(RuntimeError):
    pass


class FusekiClient:
    _RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def _post(
        self,
        url: str,
        *,
        data: Any,
        headers: dict[str, str],
        operation: str,
        payload_bytes: Optional[int] = None,
    ) -> requests.Response:
        attempts = self.retries + 1
        last_exc: Optional[BaseException] = None

        for attempt in range(1, attempts + 1):
            try:
                resp = requests.post(
                    url,
                    data=data,
                    headers=headers,
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt >= attempts:
                    break
                logger.warning(
                    "Fuseki %s transport failure attempt=%s/%s url=%s bytes=%s error=%s",
                    operation,
                    attempt,
                    attempts,
                    url,
                    payload_bytes,
                    exc,
                )
                time.sleep(0.2 * attempt)
                continue
            except requests.RequestException as exc:
                raise FusekiError(
                    f"Fuseki {operation} request failed url={url}: {exc}"
                ) from exc

            if resp.status_code < 400:
                return resp

            body = (resp.text or "").strip()
            if len(body) > 2000:
                body = body[:2000] + "...[truncated]"
            error = FusekiError(
                f"Fuseki {operation} HTTP {resp.status_code} "
                f"url={url} bytes={payload_bytes}: {body}"
            )

            if resp.status_code in self._RETRYABLE_STATUS and attempt < attempts:
                last_exc = error
                logger.warning(
                    "Fuseki %s server failure attempt=%s/%s status=%s url=%s bytes=%s body=%s",
                    operation,
                    attempt,
                    attempts,
                    resp.status_code,
                    url,
                    payload_bytes,
                    body,
                )
                time.sleep(0.2 * attempt)
                continue

            raise error

        detail = f": {last_exc}" if last_exc else ""
        raise FusekiError(
            f"Fuseki {operation} request failed after {attempts} attempts "
            f"url={url} bytes={payload_bytes}{detail}"
        ) from last_exc

    def update(self, dataset: str, sparql: str) -> None:
        url = f"{self.base_url}/{dataset}/update"
        payload = sparql.encode("utf-8")

        logger.debug(
            "Fuseki UPDATE dataset=%s bytes=%s url=%s",
            dataset,
            len(payload),
            url,
        )

        self._post(
            url,
            data=payload,
            headers={"Content-Type": "application/sparql-update; charset=utf-8"},
            operation="UPDATE",
            payload_bytes=len(payload),
        )

    def query(self, dataset: str, sparql: str) -> dict:
        url = f"{self.base_url}/{dataset}/sparql"

        logger.debug("Fuseki QUERY dataset=%s url=%s", dataset, url)

        resp = self._post(
            url,
            data={"query": sparql},
            headers={"Accept": "application/sparql+json"},
            operation="QUERY",
            payload_bytes=len(sparql.encode("utf-8")),
        )

        return resp.json()
