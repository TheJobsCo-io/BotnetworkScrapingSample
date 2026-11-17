"""
Sample scraper. Same shape as UsaJobs.py.

JbsMain does not load this file: the name "scraper" is reserved.
Copy it to SiteName.py and rename the class to SiteName to run it.

Proxy login is not stored here. Api.proxy_url_from_config reads
ProxyUsername and ProxyPassword from config.json and builds
http://user:password@host:port/
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlparse

from Helper import Api

logger = logging.getLogger(__name__)

BASE_URL = "https://example.com"
SEARCH_URL = BASE_URL + "/search"
PAGE_SIZE = 50

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ScrapeError(RuntimeError):
    """The site returned a response this scraper cannot read as job listings."""


class ProxyClient:
    def __init__(self, proxy_url: str, timeout: int) -> None:
        self.timeout = timeout
        handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        self.opener = urllib.request.build_opener(handler)

    def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json; charset=utf-8",
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as exc:
            raise ScrapeError(f"Site returned HTTP {exc.code} for {url}") from exc
        except urllib.error.URLError as exc:
            raise ScrapeError(f"Could not reach the site: {exc.reason}") from exc

        text = raw.decode(charset, errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ScrapeError("Site returned non-JSON search results.") from exc
        if not isinstance(data, dict):
            raise ScrapeError("Search payload was not an object.")
        return data


class Scraper:
    """Scrape one site and batch-save via the shared Api."""

    BOT_NAME = "Scraper"
    PAGE_SIZE = PAGE_SIZE
    REQUEST_DELAY_SECONDS = 0.4
    REQUEST_TIMEOUT_SECONDS = 60

    PROXY_HOST = "p.webshare.io"
    PROXY_PORT = 80

    def __init__(
        self,
        api: Optional[Api] = None,
        proxies: Optional[list[str]] = None,
        **kwargs,
    ) -> None:
        if proxies:
            self.proxy_url = self._pick_proxy_url(proxies)
        else:
            self.proxy_url = Api.proxy_url_from_config(self.PROXY_HOST, self.PROXY_PORT)
        self.api = api or Api(bot_name=self.BOT_NAME)
        self.api.bot_name = self.BOT_NAME
        self._stop = False

    def stop(self) -> None:
        self._stop = True
        self.api._log("Stop requested.", logging.WARNING)

    def run(self, job: dict[str, Any]) -> int:
        """
        Scrape for the claimed job and save in batches of 20.

        Does not claim or complete — JbsMain owns those around all scrapers.
        Returns how many jobs were flushed via AddJBSData.
        """
        keyword = job["keyword"]
        self.api.bot_name = self.BOT_NAME
        self.api._log(
            f"Scraping for keyword={keyword!r} country={job['country']!r} "
            f"stop_at={job['stop_at']} max_hours_ago={job['max_hours_ago']}"
        )
        try:
            total_saved = self._scrape_and_save(
                keyword=keyword,
                country=job["country"],
                stop_at=job["stop_at"],
                max_hours_ago=job["max_hours_ago"],
            )
            self.api._log(f"Scrape finished for '{keyword}': saved {total_saved}")
            return total_saved
        except Exception as exc:
            self.api._log(f"Scrape failed for '{keyword}': {exc}", logging.ERROR)
            self.api._flush_batch()
            raise

    def _scrape_and_save(
        self,
        keyword: str,
        country: str,
        stop_at: int,
        max_hours_ago: int,
    ) -> int:
        results_wanted = max(1, int(stop_at or 500))
        location = ""
        _ = country
        date_posted_days = self._hours_to_date_posted_days(max_hours_ago)

        self.api._log(f"Proxy is used: {self._proxy_host()}")
        client = ProxyClient(
            self._require_proxy(self.proxy_url),
            self.REQUEST_TIMEOUT_SECONDS,
        )
        page_size = min(self.PAGE_SIZE, max(1, results_wanted))
        pages = max(1, math.ceil(results_wanted / page_size))
        collected = 0
        seen: set[str] = set()
        total: Optional[int] = None
        total_saved = 0

        for page in range(1, pages + 1):
            if self._stop or collected >= results_wanted:
                break

            payload: dict[str, Any] = {
                "Keyword": keyword or "",
                "Page": str(page),
                "ResultsPerPage": page_size,
            }
            if date_posted_days is not None:
                payload["DatePosted"] = str(date_posted_days)
            if location:
                payload["Location"] = location

            try:
                data = client.post_json(SEARCH_URL, payload)
                found = self._parse_jobs(data)
                page_total = self._parse_total(data)
            except Exception as exc:
                self.api._log(
                    f"page {page} failed ({type(exc).__name__}: {exc}).",
                    logging.ERROR,
                )
                break

            if page == 1 and page_total is not None:
                total = page_total
                self.api._log(f"Search reports {total} jobs")

            added = 0
            for record in found:
                if self._stop or collected >= results_wanted:
                    break
                key = str(record.get("job_id") or record.get("job_url") or "")
                if not key or key in seen:
                    continue
                if not record.get("job_url"):
                    continue
                seen.add(key)
                total_saved += self.api._queue_record(record)
                collected += 1
                added += 1

            self.api._log(f"Page {page}: {added} new jobs ({collected} collected)")
            if added == 0 or not found:
                break
            if total is not None and page * page_size >= total:
                break
            if self.REQUEST_DELAY_SECONDS and collected < results_wanted:
                time.sleep(self.REQUEST_DELAY_SECONDS)

        total_saved += self.api._flush_batch()
        self.api._log(f"Jobs scrape found {collected} usable job(s) for {keyword!r}.")
        return total_saved

    @staticmethod
    def _hours_to_date_posted_days(max_hours_ago: int) -> Optional[int]:
        try:
            hours = int(max_hours_ago or 0)
        except (TypeError, ValueError):
            return None
        if hours <= 0:
            return None
        return min(60, max(1, math.ceil(hours / 24)))

    @classmethod
    def _pick_proxy_url(cls, proxies: Optional[list[str]]) -> str:
        for proxy in list(proxies or []):
            cleaned = (proxy or "").strip()
            if not cleaned or cleaned.lower() in {"localhost", "http://localhost"}:
                continue
            if "://" not in cleaned:
                cleaned = "http://" + cleaned
            return cleaned
        raise ScrapeError("No proxy configured.")

    def _proxy_host(self) -> str:
        parsed = urlparse(self.proxy_url)
        if parsed.hostname and parsed.port:
            return f"{parsed.hostname}:{parsed.port}"
        return parsed.hostname or self.PROXY_HOST

    @staticmethod
    def _require_proxy(proxy_url: str) -> str:
        parsed = urlparse(proxy_url)
        if not parsed.hostname or not parsed.port:
            raise ScrapeError("Proxy must look like http://user:password@host:port")
        return proxy_url

    @staticmethod
    def _parse_total(data: dict[str, Any]) -> Optional[int]:
        total = data.get("Total")
        try:
            return int(str(total).replace(",", ""))
        except (TypeError, ValueError):
            return None

    def _parse_jobs(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        jobs_raw = data.get("Jobs") or []
        if not isinstance(jobs_raw, list):
            return []

        records: list[dict[str, Any]] = []
        for item in jobs_raw:
            if not isinstance(item, dict):
                continue
            record = self._map_job(item)
            if record:
                records.append(record)
        return records

    def _map_job(self, item: dict[str, Any]) -> Optional[dict[str, Any]]:
        title = self._clean_text(str(item.get("Title") or ""))
        job_id = self._clean_text(str(item.get("Id") or ""))
        job_url = self._clean_text(str(item.get("Url") or ""))
        if job_url.startswith("/"):
            job_url = BASE_URL + job_url
        if not title or not job_url:
            return None

        return {
            "title": title,
            "company": self._clean_text(str(item.get("Company") or "")),
            "location": self._clean_text(str(item.get("Location") or "")),
            "schedule": "",
            "work_type": "",
            "salary": self._clean_text(str(item.get("Salary") or "")),
            "salary_min": self._as_number(item.get("SalaryMin")),
            "date_posted": self._clean_text(str(item.get("DatePosted") or "")),
            "closing_date": "",
            "department": "",
            "job_number": job_id,
            "job_grade": "",
            "hiring_path": "",
            "job_category": "",
            "relocation": "",
            "description": self._clean_text(str(item.get("Description") or "")),
            "job_url": job_url,
            "apply_url": job_url,
            "job_id": job_id,
            "site": "example",
            "source": "example",
            "job_site": f"{BASE_URL}/",
        }

    @staticmethod
    def _clean_text(value: str) -> str:
        return " ".join((value or "").split()).strip()

    @staticmethod
    def _as_number(value: Any) -> Optional[float | int]:
        if value is None or value == "" or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        text = str(value).replace(",", "").strip()
        try:
            number = float(text)
        except ValueError:
            return None
        if number.is_integer():
            return int(number)
        return number
