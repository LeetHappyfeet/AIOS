# aios/rdf/fuseki.py

import time
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)


class FusekiError(RuntimeError):
    pass


class FusekiClient:
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

    # -----------------------------
    # Internal helper
    # -----------------------------

    def _post(self, url: str, data: dict, headers: dict) -> requests.Response:
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.retries + 2):
            try:
                resp = requests.post(
                    url,
                    data=data,
                    headers=headers,
                    timeout=self.timeout,
                )

                if resp.status_code >= 400:
                    raise FusekiError(
                        f"Fuseki HTTP {resp.status_code}: {resp.text}"
                    )

                return resp

            except Exception as e:
                last_exc = e
                logger.warning(
                    "Fuseki request failed (attempt %s/%s): %s",
                    attempt,
                    self.retries + 1,
                    e,
                )
                time.sleep(0.2 * attempt)

        raise FusekiError("Fuseki request failed after retries") from last_exc

    # -----------------------------
    # Public API
    # -----------------------------

    def update(self, dataset: str, sparql: str) -> None:
        url = f"{self.base_url}/{dataset}/update"

        logger.debug("Fuseki UPDATE → %s", url)

        self._post(
            url,
            data={"update": sparql},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def query(self, dataset: str, sparql: str) -> dict:
        url = f"{self.base_url}/{dataset}/sparql"

        logger.debug("Fuseki QUERY → %s", url)

        resp = self._post(
            url,
            data={"query": sparql},
            headers={"Accept": "application/sparql+json"},
        )

        return resp.json()
