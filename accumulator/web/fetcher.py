# accumulator/web/fetcher.py

import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException

from .config import DEFAULT_USER_AGENT


class SeleniumFetcher:
    def __init__(self):
        self.driver = None

    def _ensure_driver(self):
        if self.driver is not None:
            return

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument(f"user-agent={DEFAULT_USER_AGENT}")

        self.driver = webdriver.Chrome(options=opts)
        self.driver.set_page_load_timeout(20)
        self.driver.set_script_timeout(20)

    def fetch(self, url: str) -> dict:
        self._ensure_driver()

        start = time.time()
        try:
            self.driver.get(url)
        except TimeoutException:
            raise

        html = self.driver.page_source
        load_time_ms = int((time.time() - start) * 1000)

        return {
            "html": html,
            "load_time_ms": load_time_ms,
            "rendered": True,
            "user_agent": DEFAULT_USER_AGENT,
        }

    def close(self):
        if self.driver:
            self.driver.quit()
            self.driver = None
