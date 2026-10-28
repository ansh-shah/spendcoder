"""Deterministic offline backend for the demo, tests and the GEPA demo.

Quality depends on the wording of the criteria: an option scores by how many words
it shares with the transaction, so rewording a category guide changes accuracy.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import BackendError, Ledger, validate_chat

STOPWORDS = frozenset(
    "the and for with any not from this that are was has its use other none default keywords scope rule".split()
)
UNKNOWN = "Unknown"

_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
    "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
_PREFIX = re.compile(r"^\s*(?:SQ\s*\*|TST\s*\*|SP\s*\*|PP\s*\*|POS\b)\s*", re.IGNORECASE)


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 3 and t not in STOPWORDS]


def token_set(text: str) -> set[str]:
    toks = tokens(text)
    return set(toks) | {t[:5] for t in toks if len(t) >= 6}


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, (list, tuple)):
        return [s for v in obj for s in _strings(v)]
    return []


def clean_descriptor(desc: str) -> str:
    text = _PREFIX.sub("", desc or "")
    text = re.sub(r"#\s*\d+", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    if words and len(words[-1]) == 2 and words[-1].isupper() and words[-1] in _US_STATES:
        words.pop()
        if len(words) >= 3:
            words.pop()
    words = [w.capitalize() if w.isupper() or w.islower() else w for w in words]
    return " ".join(words) or (desc or "").strip().title()


class MockBackend:
    name = "mock"

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    # -------------------------------------------------------------- decide
    def _choice(self, q: dict, state_toks: set[str]) -> dict:
        criteria: dict[str, str] = q.get("criteria") or {}
        options = list(criteria)
        scores = {
            o: 0 if o == UNKNOWN else len(state_toks & token_set(f"{o} {criteria[o] or ''}")) for o in options
        }
        best = max(scores.values()) if scores else 0
        winners = [o for o in options if scores[o] == best]
        if best == 0:
            chosen = UNKNOWN if UNKNOWN in criteria else options[0]
            p = 0.8
        else:
            chosen = winners[0]
            if len(winners) > 1:
                p = 0.45
            else:
                second = max((s for o, s in scores.items() if o != chosen), default=0)
                p = 0.9 if best - second >= 2 else 0.7
        others = [o for o in options if o != chosen]
        if others:
            probs = {o: (1.0 - p) / len(others) for o in others}
            probs[chosen] = p
        else:
            probs, p = {chosen: 1.0}, 1.0
        return {"type": "choice", "choice": chosen, "probabilities": probs, "confidence": p}

    def _noul(self, q: dict, state: dict) -> dict:
        stems = {t[:5] for t in tokens(str(q.get("instructions", ""))) if len(t) >= 6}
        note = state.get("note") if isinstance(state, dict) else None
        text = note if isinstance(note, str) and note.strip() else " ".join(_strings(state))
        toks = tokens(text)
        hit = any(t.startswith(s) for s in stems for t in toks)
        return {"type": "noul", "noul": 0.9 if hit else 0.1}

    def decide(self, *, state: dict, questions: dict, purpose: str) -> dict:
        state_toks = token_set(" ".join(_strings(state)))
        answers: dict[str, dict] = {}
        for qid, q in questions.items():
            kind = q["type"]
            if kind == "choice":
                answers[qid] = self._choice(q, state_toks)
            elif kind == "noul":
                answers[qid] = self._noul(q, state)
            else:
                answers[qid] = {"type": "score", "score": 0, "confidence": 1.0}
        self.ledger.record(purpose)
        return answers

    # ----------------------------------------------------------- chat_json
    def chat_json(self, *, system: str, user: str, schema: dict, schema_name: str, purpose: str) -> dict:
        try:
            payload = json.loads(user)
        except json.JSONDecodeError as e:
            raise BackendError(f"mock backend needs a JSON user message: {e}") from e
        if purpose == "normalize":
            result: dict = {"results": [
                {"key": it["key"],
                 "vendor": it.get("reference_vendor") or clean_descriptor(it.get("description", ""))}
                for it in payload.get("items", [])
            ]}
        elif purpose == "review":
            reviews = []
            for g in payload.get("groups", []):
                current = g.get("current_rule_id") or ""
                legal = {o.get("rule_id") for o in g.get("legal_options", [])}
                keep = bool(current) and current in legal
                reviews.append({
                    "group_id": g["group_id"],
                    "action": "keep" if keep else "unresolved",
                    "rule_id": current,
                    "reason": "mock reviewer: current rule is legal" if keep
                    else "mock reviewer: no legal current rule, needs a person",
                })
            result = {"reviews": reviews}
        else:
            raise BackendError(f"mock backend does not implement chat purpose {purpose!r}")
        self.ledger.record(purpose)
        return validate_chat(result, schema)

    def chat_text(self, *, prompt: str, purpose: str) -> str:
        self.ledger.record(purpose)
        return "mock: " + prompt[:200]
