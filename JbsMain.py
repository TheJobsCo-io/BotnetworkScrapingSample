"""
Jobs bot entry point.

Discovers every scraper .py and runs them one by one for a shared claim:

  claim → LinkedIn.run (scrape + batch save) → Rozee.run (...) → complete → wait
"""

from __future__ import annotations

import importlib
import logging
import random
import time
from pathlib import Path
from typing import Any, Optional

from Helper import Api

logger = logging.getLogger(__name__)


class JbsMain:
    """Claim once, run each scraper file, then complete the OutRequest."""

    NO_KEYWORD_WAIT_SECONDS = 5 * 60
    POST_CLAIM_MIN_WAIT_SECONDS = 10 * 60
    POST_CLAIM_MAX_WAIT_SECONDS = 15 * 60
    SCRAPERS_DIR = Path(__file__).resolve().parent
    RESERVED_MODULES = frozenset(
        {
            "jbsmain",
            "helper",
            "config",
            "__init__",
            "scraper",
            "requirements",
        }
    )
    _logging_configured = False

    def __init__(self) -> None:
        self._stop = False
        self._current: Optional[Any] = None
        self._api: Optional[Api] = None

    @classmethod
    def configure_logging(cls, level: int = logging.INFO) -> None:
        if cls._logging_configured:
            return
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        cls._logging_configured = True
        logger.info("Starting JbsMain V3")

    def stop(self) -> None:
        self._stop = True
        if self._current is not None and hasattr(self._current, "stop"):
            self._current.stop()
        logger.warning("[JbsMain] Stop requested.")

    def _discover_scrapers(self) -> list[type]:
        """Load scraper classes that expose run(api, job)."""
        paths = sorted(
            (
                path
                for path in self.SCRAPERS_DIR.glob("*.py")
                if path.stem.lower() not in self.RESERVED_MODULES
                and not path.stem.startswith("_")
            ),
            key=lambda path: path.stem.lower(),
        )
        scrapers: list[type] = []
        for path in paths:
            logger.info("[JbsMain] Loading scraper file %s", path.name)
            module = importlib.import_module(path.stem)
            scrapers.append(self._scraper_class_from_module(module, path.stem))

        if not scrapers:
            raise RuntimeError(
                f"No scraper python files found in {self.SCRAPERS_DIR}."
            )
        return scrapers

    @staticmethod
    def _scraper_class_from_module(module: Any, module_name: str) -> type:
        preferred = module_name.lower()
        found: list[type] = []
        for attr_name, obj in vars(module).items():
            if attr_name.startswith("_") or not isinstance(obj, type):
                continue
            if getattr(obj, "__module__", None) != module.__name__:
                continue
            if not callable(getattr(obj, "run", None)):
                continue
            if attr_name.lower() == preferred:
                return obj
            found.append(obj)

        if len(found) == 1:
            return found[0]

        raise AttributeError(
            f"Module '{module_name}' must define a scraper class with a run() method."
        )

    @staticmethod
    def _protocol_fields_to_dict(fields: Optional[list]) -> dict[str, str]:
        return {
            item["fieldName"]: item["protocolFieldValue"]
            for item in (fields or [])
            if item.get("fieldName") is not None
        }

    @staticmethod
    def _parse_int_field(
        fields: dict[str, str],
        field_name: str,
        default: int,
    ) -> int:
        value = fields.get(field_name)
        if value in (None, ""):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _begin_claim(self, api: Api, claim: dict) -> dict[str, Any]:
        fields = self._protocol_fields_to_dict(claim.get("protocolsFieldsValue"))
        filename = fields.get("Filename") or fields.get("FileName") or ""
        country = (fields.get("Country") or "USA").strip()
        stop_at = self._parse_int_field(fields, "StopAt", 500)
        max_hours_ago = self._parse_int_field(fields, "MaxHoursAgo", 4)
        keyword = claim["keyword"]

        api._keyword = keyword
        api._current_id_out_request = claim.get("idOutRequest")
        api._save_batch = []
        api._log(
            f"Protocol fields: Filename={filename!r}, Country={country!r}, "
            f"StopAt={stop_at}, MaxHoursAgo={max_hours_ago}"
        )
        return {
            "keyword": keyword,
            "country": country,
            "stop_at": stop_at,
            "max_hours_ago": max_hours_ago,
        }

    def _claim_until_available(self, api: Api) -> Optional[dict[str, Any]]:
        """Claim a keyword, waiting when none are available."""
        while not self._stop:
            api.bot_name = "JbsMain"
            claim = api.claim_out_request()
            if claim:
                return self._begin_claim(api, claim)

            api._log(
                f"No keyword available. Waiting "
                f"{self.NO_KEYWORD_WAIT_SECONDS // 60} minutes...",
                logging.WARNING,
            )
            time.sleep(self.NO_KEYWORD_WAIT_SECONDS)
        return None

    def run_continuous(self) -> None:
        self.configure_logging()
        api = Api(bot_name="JbsMain")
        self._api = api
        api._disable_ssl_warnings()
        api._log("JBS bot started.")

        scrapers = self._discover_scrapers()
        names = ", ".join(f"{cls.__module__}.py" for cls in scrapers)
        api._log(f"Running scraper files one by one: {names}")

        while not self._stop:
            # 1) claim once for this round
            # Production: uncomment the claim below and comment the hardcoded block.
            # job = self._claim_until_available(api)
            job = self._begin_claim(
                api,
                {
                    "idOutRequest": 12345,
                    "keyword": "software engineer",
                    "protocolsFieldsValue": [
                        {"fieldName": "Country", "protocolFieldValue": "USA"},
                        {"fieldName": "StopAt", "protocolFieldValue": "500"},
                        {"fieldName": "MaxHoursAgo", "protocolFieldValue": "4"},
                    ],
                },
            )
            if job is None or self._stop:
                break

            keyword = job["keyword"]
            try:
                # 2) each scraper: scrape + queue/flush every 20 (no complete)
                for scraper_cls in scrapers:
                    if self._stop:
                        break
                    file_name = scraper_cls.__module__
                    api.bot_name = file_name
                    api._log(f"Starting {file_name}.py for keyword={keyword!r}")
                    scraper = scraper_cls(api=api)
                    self._current = scraper
                    try:
                        scraper.run(job)
                    except Exception as exc:
                        api._log(
                            f"{file_name}.run() failed: {exc}",
                            logging.ERROR,
                        )
                        api._flush_batch()
                    finally:
                        self._current = None
                        api._flush_batch()
            finally:
                # 3) complete once after all scrapers (LinkedIn, Rozee, ...)
                api.bot_name = "JbsMain"
                api._flush_batch()
                api._complete_out_request()
                api._current_id_out_request = None
                api._keyword = ""
                api._save_batch = []

            if self._stop:
                break

            # 4) wait, then repeat
            delay = random.uniform(
                self.POST_CLAIM_MIN_WAIT_SECONDS,
                self.POST_CLAIM_MAX_WAIT_SECONDS,
            )
            api._log(
                f"Finished keyword '{keyword}'. "
                f"Waiting {delay / 60:.1f} minutes before next claim..."
            )
            time.sleep(delay)

        api.bot_name = "JbsMain"
        api._log("Bot stopped.", logging.WARNING)

    def run(self) -> None:
        self.run_continuous()


if __name__ == "__main__":
    bot = JbsMain()
    try:
        bot.run()
    except KeyboardInterrupt:
        bot.stop()
