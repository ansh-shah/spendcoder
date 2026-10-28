"""Backend contract shared by every LLM backend (OpenRouter, agent-files, mock).

Two primitives:

* ``chat_json``  - generative call with a strict JSON schema (vendor normalization,
  review). ``user`` is always a JSON document so every backend can parse it.
* ``decide``     - calibrated judgment through a Decisions-style API: a ``state``
  object plus named ``questions`` (``choice`` / ``noul`` / ``score``). Returns
  probabilities, never prose.

Plus ``chat_text`` for free-form text (used only by GEPA reflection).

Every answer is validated here before a stage may use it.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol


class BackendError(RuntimeError):
    """Unrecoverable backend failure (auth, bad request, exhausted retries)."""


class InvalidAnswer(ValueError):
    """A model answer failed schema / contract validation."""


class Backend(Protocol):
    name: str

    def chat_json(self, *, system: str, user: str, schema: dict, schema_name: str, purpose: str) -> dict: ...

    def decide(self, *, state: dict, questions: dict, purpose: str) -> dict: ...

    def chat_text(self, *, prompt: str, purpose: str) -> str: ...


@dataclass
class Ledger:
    """Thread-safe call/cost accounting, written to the run manifest."""
    calls: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, purpose: str, *, cost: float = 0.0, input_tokens: int = 0, output_tokens: int = 0) -> None:
        with self._lock:
            self.calls[purpose] = self.calls.get(purpose, 0) + 1
            self.cost_usd += float(cost or 0.0)
            self.input_tokens += int(input_tokens or 0)
            self.output_tokens += int(output_tokens or 0)

    def hit(self) -> None:
        with self._lock:
            self.cache_hits += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": dict(self.calls),
            "cost_usd": round(self.cost_usd, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hits": self.cache_hits,
        }


# --------------------------------------------------------------------------- #
#  Minimal JSON-schema validator (the subset spendcoder's schemas use)
# --------------------------------------------------------------------------- #
_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


def schema_errors(obj: Any, schema: dict, path: str = "$") -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        ok = False
        for name in types:
            py = _TYPES[name]
            if name in ("integer", "number") and isinstance(obj, bool):
                continue
            if isinstance(obj, py):
                ok = True
                break
        if not ok:
            return [f"{path}: expected {t}, got {type(obj).__name__}"]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} not in enum")
    if isinstance(obj, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in obj:
                errs.append(f"{path}: missing {req!r}")
        if schema.get("additionalProperties") is False:
            for k in obj:
                if k not in props:
                    errs.append(f"{path}: unexpected key {k!r}")
        for k, sub in props.items():
            if k in obj:
                errs.extend(schema_errors(obj[k], sub, f"{path}.{k}"))
    if isinstance(obj, list) and "items" in schema:
        for i, item in enumerate(obj):
            errs.extend(schema_errors(item, schema["items"], f"{path}[{i}]"))
    return errs


def validate_chat(obj: Any, schema: dict) -> dict:
    errs = schema_errors(obj, schema)
    if errs:
        raise InvalidAnswer("; ".join(errs[:8]))
    return obj


def validate_decision(answers: Any, questions: dict) -> dict:
    """Every question answered, with its declared type and a legal value."""
    if not isinstance(answers, dict):
        raise InvalidAnswer("answers must be an object keyed by question id")
    for qid, q in questions.items():
        a = answers.get(qid)
        if not isinstance(a, dict):
            raise InvalidAnswer(f"missing answer for {qid!r}")
        qtype = q["type"]
        if a.get("type") != qtype:
            raise InvalidAnswer(f"{qid}: expected type {qtype}, got {a.get('type')!r}")
        if qtype == "choice":
            if a.get("choice") not in q["criteria"]:
                raise InvalidAnswer(f"{qid}: choice {a.get('choice')!r} is not an option")
        elif qtype == "noul":
            p = a.get("noul")
            if not isinstance(p, (int, float)) or isinstance(p, bool) or not 0.0 <= p <= 1.0:
                raise InvalidAnswer(f"{qid}: noul must be a probability in [0, 1]")
        elif qtype == "score":
            if not isinstance(a.get("score"), (int, float)):
                raise InvalidAnswer(f"{qid}: score must be numeric")
    return answers


def choice_confidence(answer: dict) -> float:
    """Probability mass on the selected option (falls back to `confidence`)."""
    probs = answer.get("probabilities") or {}
    if answer.get("choice") in probs:
        return float(probs[answer["choice"]])
    return float(answer.get("confidence", 0.0) or 0.0)
