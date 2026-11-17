"""
API client for the jobs bot.

Claim an OutRequest, post scraped rows, and mark the request complete.
"""

from __future__ import annotations

import json
import logging
import math
import urllib3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class Api:
    VERSION = "1"
    PROTOCOL_LABEL = "JBS v1"
    JOB_SITE_URLS = {
        "linkedin": "https://www.linkedin.com/",
        "indeed": "https://www.indeed.com/",
        "zip_recruiter": "https://www.ziprecruiter.com/",
        "ziprecruiter": "https://www.ziprecruiter.com/",
        "glassdoor": "https://www.glassdoor.com/",
        "usajobs": "https://www.usajobs.gov/",
        "governmentjobs": "https://www.governmentjobs.com/",
        "wellfound": "https://wellfound.com/",
        "talent": "https://www.talent.com/",
        "jobcase": "https://www.jobcase.com/",
        "simplyhired": "https://www.simplyhired.com/",
        "careerjet": "https://www.careerjet.com/",
        "welcometothejungle": "https://www.welcometothejungle.com/",
        "ycombinator": "https://www.ycombinator.com/",
        "yc": "https://www.ycombinator.com/",
    }

    CLAIM_API_URL = "https://192.168.1.195:8443/api/ClaimJBSOutRequest"
    COMPLETE_OUT_REQUEST_API_URL = (
        "https://192.168.1.195:8443/api/CompleteJBSOutRequest"
    )
    ADD_JBS_DATA_API_URL = "https://192.168.1.195:8443/api/BotNetwork/AddJBSData"

    CONFIG_FILE = Path(__file__).resolve().parent / "config.json"
    SAVE_BATCH_SIZE = 20

    _warnings_disabled = False
    _api_key: Optional[str] = None

    def __init__(self, bot_name: str = "JbsMain"):
        # Overridden by JbsMain to the scraper file stem while that file runs.
        self.bot_name = bot_name
        self._current_id_out_request: Optional[int] = None
        self._keyword = ""
        self._save_batch: list[dict] = []

    @classmethod
    def _disable_ssl_warnings(cls) -> None:
        if not cls._warnings_disabled:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            cls._warnings_disabled = True

    @classmethod
    def _get_api_key(cls) -> str:
        if cls._api_key is not None:
            return cls._api_key

        if not cls.CONFIG_FILE.is_file():
            raise FileNotFoundError(
                f"API key config not found: {cls.CONFIG_FILE}. "
                "Create it with an 'APIKey' field."
            )

        with cls.CONFIG_FILE.open(encoding="utf-8") as config_file:
            config = json.load(config_file)

        api_key = config.get("APIKey") or config.get("api_key")
        if not api_key:
            raise ValueError(f"Missing 'APIKey' in {cls.CONFIG_FILE}")

        cls._api_key = str(api_key).strip()
        return cls._api_key

    def _log(self, message: str, level: int = logging.INFO) -> None:
        logger.log(level, "[%s] %s", self.bot_name, message)

    def claim_out_request(self) -> Optional[dict]:
        self._disable_ssl_warnings()
        self._log("Claiming next OutRequest...")

        try:
            response = requests.post(
                self.CLAIM_API_URL,
                headers={
                    "APIKey": self._get_api_key(),
                    "Content-Type": "application/json",
                },
                json={},
                verify=False,
                timeout=30,
            )
        except requests.RequestException as exc:
            self._log(f"Claim API request failed: {exc}", logging.ERROR)
            return None

        if response.status_code != 200:
            self._log(
                f"No keyword available (HTTP {response.status_code}).",
                logging.WARNING,
            )
            return None

        try:
            data = response.json()
        except json.JSONDecodeError:
            self._log("Claim API returned invalid JSON.", logging.ERROR)
            return None

        keyword = data.get("keyword")
        if not keyword:
            self._log("Claim API returned no keyword.", logging.WARNING)
            return None

        self._log(
            f"Claim succeeded: OutRequest={data.get('idOutRequest')}, "
            f"keyword='{keyword}', protocol={data.get('idProtocol')}, "
            f"job={data.get('idScrapingJob')}"
        )
        return data

    @staticmethod
    def _first_present(record: dict, *keys: str) -> Optional[str]:
        for key in keys:
            value = record.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text and text.lower() != "none":
                return text
        return None

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        """
        Make values safe for JSON / .NET APIs.
        NaN/Infinity and numpy floats often cause 'float' parse errors server-side.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return int(value)
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None
            return float(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(k): cls._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._json_safe(v) for v in value]
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return value

        # numpy / pandas scalars
        item = getattr(value, "item", None)
        if callable(item):
            try:
                return cls._json_safe(item())
            except Exception:
                pass

        typ = type(value).__name__
        module = type(value).__module__ or ""
        if "numpy" in module or "pandas" in module:
            try:
                if value != value:  # NaN
                    return None
            except Exception:
                pass
            try:
                as_float = float(value)
                if math.isnan(as_float) or math.isinf(as_float):
                    return None
                if typ.startswith("int") or typ.endswith("int64") or typ.endswith("int32"):
                    return int(as_float)
                return as_float
            except Exception:
                return str(value)

        return str(value)

    @classmethod
    def _drop_nulls(cls, value: Any) -> Any:
        """Remove null/empty values so inserts don't look like failed scrapes."""
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                item = cls._drop_nulls(item)
                if item is None:
                    continue
                if item == "" or item == [] or item == {}:
                    continue
                cleaned[key] = item
            return cleaned
        if isinstance(value, list):
            return [
                item
                for item in (cls._drop_nulls(v) for v in value)
                if item is not None and item != "" and item != [] and item != {}
            ]
        return value

    def _build_job_payload(self, record: dict) -> dict:
        """Map a scraped job record to the AddJBSData schema."""
        safe_record = self._json_safe(record) or {}
        if not isinstance(safe_record, dict):
            safe_record = {"value": safe_record}

        # JobSpy uses company_name in some paths; dataframe uses company
        if not safe_record.get("company") and safe_record.get("company_name"):
            safe_record["company"] = safe_record["company_name"]

        job_url = self._first_present(
            safe_record, "job_url", "job_url_direct", "url", "Job Url"
        )
        title = self._first_present(safe_record, "title", "Job Title")
        company = self._first_present(
            safe_record, "company", "company_name", "Company"
        )
        location = self._first_present(
            safe_record, "location", "Location", "city"
        )
        posted_at = self._first_present(
            safe_record,
            "date_posted",
            "date",
            "Posted At",
        )
        job_site = self._job_site_url(safe_record)

        raw_data = self._drop_nulls(safe_record)

        return {
            "Function Protocol and Version": self.PROTOCOL_LABEL,
            "Job Url": job_url or "",
            "Raw Data": raw_data if isinstance(raw_data, dict) else {},
            "search_text": self._keyword,
            "Job Title": title or "",
            "Company": company or "",
            "Location": location or "",
            "Posted At": posted_at or "",
            "Job Site": job_site or "",
        }

    @classmethod
    def _job_site_url(cls, record: dict) -> str:
        """Board homepage, e.g. https://www.linkedin.com/."""
        explicit = cls._first_present(
            record, "Job Site", "job_site", "job_site_url"
        )
        if explicit:
            return explicit
        site_name = cls._first_present(record, "site", "source", "site_name") or ""
        key = site_name.strip().lower().replace(" ", "_").replace("-", "_")
        return cls.JOB_SITE_URLS.get(key, "")

    def _valid_id_out_request(self) -> bool:
        try:
            return int(self._current_id_out_request) > 0
        except (TypeError, ValueError):
            return False

    def _add_jbs_data(self, rows: list[dict]) -> bool:
        """POST a batch of jobs (up to SAVE_BATCH_SIZE). data is a list."""
        if not rows:
            return True
        if not self._valid_id_out_request():
            self._log(
                "Skipping AddJBSData: idOutRequest must be a positive integer.",
                logging.WARNING,
            )
            return False

        payload = self._json_safe(
            {
                "idOutRequest": int(self._current_id_out_request),
                "data": rows,
            }
        )
        # allow_nan=False prevents invalid JSON NaN/Infinity that .NET rejects
        body = json.dumps(payload, allow_nan=False, default=str)
        self._disable_ssl_warnings()
        self._log(
            f"Sending AddJBSData batch: idOutRequest={self._current_id_out_request}, "
            f"rows={len(rows)}"
        )

        print(body)
        return True
        #  Zahoor will use this code to add the data to the database
        # try:
        #     response = requests.post(
        #         self.ADD_JBS_DATA_API_URL,
        #         headers={
        #             "APIKey": self._get_api_key(),
        #             "Content-Type": "application/json",
        #         },
        #         data=body,
        #         verify=False,
        #         timeout=60,
        #     )
        #     if response.status_code >= 400:
        #         self._log(
        #             f"AddJBSData failed: HTTP {response.status_code}: {response.text}",
        #             logging.ERROR,
        #         )
        #         return False
        #
        #     self._log(
        #         f"AddJBSData batch saved: rows={len(rows)}, "
        #         f"HTTP {response.status_code}"
        #     )
        #     return True
        # except requests.RequestException as exc:
        #     self._log(f"AddJBSData request failed: {exc}", logging.ERROR)
        #     return False

    def _flush_batch(self) -> int:
        if not self._save_batch:
            return 0
        batch = self._save_batch
        self._save_batch = []
        if self._add_jbs_data(batch):
            return len(batch)
        self._save_batch = batch + self._save_batch
        return 0

    def _queue_record(self, record: dict) -> int:
        """Queue one job; flush automatically every SAVE_BATCH_SIZE records."""
        row = self._build_job_payload(record)
        if not row.get("Job Url"):
            self._log(
                f"Skipping job without Job Url: {row.get('Job Title')!r}",
                logging.WARNING,
            )
            return 0
        if not row.get("Job Title") and not row.get("Company"):
            self._log(
                f"Skipping job with empty title/company: {row.get('Job Url')!r}",
                logging.WARNING,
            )
            return 0

        self._save_batch.append(row)
        if len(self._save_batch) >= self.SAVE_BATCH_SIZE:
            return self._flush_batch()
        return 0

    def _save_records(self, records: list[dict]) -> int:
        saved = 0
        for record in records:
            saved += self._queue_record(record)
        saved += self._flush_batch()
        return saved

    def _complete_out_request(self) -> None:
        if not self._valid_id_out_request():
            self._log(
                "Skipping CompleteJBSOutRequest: idOutRequest must be a "
                "positive integer.",
                logging.WARNING,
            )
            return

        payload = {"idOutRequest": int(self._current_id_out_request)}
        self._disable_ssl_warnings()

        try:
            response = requests.post(
                self.COMPLETE_OUT_REQUEST_API_URL,
                headers={
                    "APIKey": self._get_api_key(),
                    "Content-Type": "application/json",
                },
                json=payload,
                verify=False,
                timeout=30,
            )
            if response.status_code >= 400:
                self._log(
                    f"CompleteJBSOutRequest failed for OutRequest "
                    f"{self._current_id_out_request}: HTTP "
                    f"{response.status_code}: {response.text}",
                    logging.ERROR,
                )
                return

            self._log(
                f"CompleteJBSOutRequest succeeded for OutRequest "
                f"{self._current_id_out_request}"
            )
        except requests.RequestException as exc:
            self._log(f"CompleteJBSOutRequest request failed: {exc}", logging.ERROR)
