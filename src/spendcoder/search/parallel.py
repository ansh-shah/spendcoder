"""Merchant lookup through Parallel's Search API. Best-effort: never fails a run."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Protocol

from ..config import Config, SearchConfig
from ..envfile import load_dotenv
from ..llm.base import BackendError, Ledger

SEARCH_URL = "https://api.parallel.ai/v1/search"
TIMEOUT_SEC = 60.0
MAX_RETRIES = 3


class SearchClient(Protocol):
    name: str

    def lookup(self, descriptor: str, note: str = "") -> list[dict]: ...


class NullSearch:
    name = "none"

    def lookup(self, descriptor: str, note: str = "") -> list[dict]:
        return []


class ParallelSearch:
    name = "parallel"

    def __init__(self, cfg: SearchConfig, ledger: Ledger, cache_dir: Path, api_key: str | None = None) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.cache_dir = Path(cache_dir) / "search"
        self.api_key = api_key or os.environ.get("PARALLEL_API_KEY", "")
        if not self.api_key:
            raise BackendError("PARALLEL_API_KEY is not set (put it in .env), or set [search] provider = \"none\".")

    def _post_json(self, url: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}
        last: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
                last = BackendError(f"HTTP {e.code} from {url}: {text[:300]}")
                if not (e.code in (408, 429) or e.code >= 500):
                    raise last from e
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, json.JSONDecodeError) as e:
                last = BackendError(f"search request failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(min(30.0, 2.0 ** attempt) * (0.5 + random.random()))
        raise BackendError(f"giving up on {url}: {last}")

    def _cache_path(self, descriptor: str) -> Path:
        key = hashlib.sha256(descriptor.encode("utf-8")).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.json"

    def lookup(self, descriptor: str, note: str = "") -> list[dict]:
        path = self._cache_path(descriptor)
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            self.ledger.hit()
            return cached
        except (OSError, ValueError):
            pass
        body = {
            "objective": f"Identify the business behind the card statement descriptor '{descriptor}': "
                         "its name and what it sells.",
            "search_queries": [f"{descriptor} merchant", f"{descriptor} company"],
            "mode": self.cfg.mode,
            "advanced_settings": {
                "max_results": self.cfg.max_results,
                "excerpt_settings": {"max_chars_per_result": self.cfg.max_chars_per_result},
            },
        }
        try:
            resp = self._post_json(SEARCH_URL, body)
            limit = self.cfg.max_chars_per_result
            results = [
                {"title": r.get("title") or "", "url": r.get("url") or "",
                 "excerpt": " ".join(str(x) for x in (r.get("excerpts") or []))[:limit]}
                for r in (resp.get("results") or [])[: self.cfg.max_results]
            ]
        except Exception:  # best effort: search must never fail a run
            self.ledger.record("search_error")
            return []
        self.ledger.record("search")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return results


def make_search(cfg: Config, ledger: Ledger) -> SearchClient:
    if cfg.search.provider != "parallel":
        return NullSearch()
    load_dotenv(cfg.root)
    if not os.environ.get("PARALLEL_API_KEY"):
        print("spendcoder: PARALLEL_API_KEY not set; web search disabled for this run.", file=sys.stderr)
        return NullSearch()
    return ParallelSearch(cfg.search, ledger, cfg.cache_dir)
