"""On-disk response cache around any backend (chat_json and decide only)."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from .base import Backend, Ledger


class CachedBackend:
    def __init__(self, inner: Backend, cache_dir: Path, ledger: Ledger) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self.ledger = ledger
        self.name = inner.name

    @property
    def cfg(self) -> Any:
        return getattr(self.inner, "cfg", None)

    def _model(self, attr: str) -> str:
        return str(getattr(self.cfg, attr, None) or self.inner.name)

    def _path(self, kind: str, material: dict) -> Path:
        key = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return self.cache_dir / kind / key[:2] / f"{key}.json"

    def _load(self, path: Path) -> Any | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))["value"]
        except (OSError, ValueError, KeyError):
            return None

    def _store(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps({"value": value}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def chat_json(self, *, system: str, user: str, schema: dict, schema_name: str, purpose: str) -> dict:
        path = self._path("chat", {"kind": "chat", "model": self._model("chat_model"),
                                   "system": system, "user": user, "schema": schema})
        hit = self._load(path)
        if hit is not None:
            self.ledger.hit()
            return hit
        out = self.inner.chat_json(system=system, user=user, schema=schema, schema_name=schema_name, purpose=purpose)
        self._store(path, out)
        return out

    def decide(self, *, state: dict, questions: dict, purpose: str) -> dict:
        path = self._path("decision", {"kind": "decision", "model": self._model("decision_model"),
                                       "state": state, "questions": questions})
        hit = self._load(path)
        if hit is not None:
            self.ledger.hit()
            return hit
        out = self.inner.decide(state=state, questions=questions, purpose=purpose)
        self._store(path, out)
        return out

    def chat_text(self, *, prompt: str, purpose: str) -> str:
        return self.inner.chat_text(prompt=prompt, purpose=purpose)

    def __getattr__(self, item: str) -> Any:  # e.g. list_decision_models
        if item == "inner":
            raise AttributeError(item)
        return getattr(self.inner, item)
