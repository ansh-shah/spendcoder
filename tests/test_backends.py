"""Backend tests: offline only (no network)."""
from __future__ import annotations

import json
import threading
import time

import pytest

from spendcoder.config import LLMConfig, SearchConfig
from spendcoder.llm.agent_files import AgentFilesBackend, queue_answer, queue_list, queue_show
from spendcoder.llm.base import InvalidAnswer, Ledger, validate_decision
from spendcoder.llm.cache import CachedBackend
from spendcoder.llm.mock import MockBackend
from spendcoder.llm.openrouter import OpenRouterBackend
from spendcoder.search.parallel import NullSearch, ParallelSearch

NORMALIZE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "required": ["key", "vendor"],
        "properties": {"key": {"type": "string"}, "vendor": {"type": "string"}}}}},
}
CRITERIA = {
    "Office Supplies": "paper pens toner staples office supplies",
    "Travel": "airline hotel lodging flights travel",
    "Unknown": "None of the listed categories clearly applies.",
}


# ----------------------------------------------------------------- mock
def test_mock_choice_follows_criteria_wording():
    mock = MockBackend(Ledger())
    q = {"cat": {"type": "choice", "instructions": "x", "criteria": CRITERIA}}
    out = mock.decide(state={"description": "STAPLES 123", "note": "toner and paper"}, questions=q, purpose="t")
    assert out["cat"]["choice"] == "Office Supplies"
    assert out["cat"]["probabilities"]["Office Supplies"] > 0.5
    assert abs(sum(out["cat"]["probabilities"].values()) - 1.0) < 1e-9
    validate_decision(out, q)

    # reword the guide: same transaction now loses its signal
    worse = {"Office Supplies": "stuff", "Travel": "trips", "Unknown": "None apply."}
    out = mock.decide(state={"note": "toner and paper"},
                      questions={"cat": {"type": "choice", "instructions": "x", "criteria": worse}}, purpose="t")
    assert out["cat"]["choice"] == "Unknown"


def test_mock_noul_and_score_and_ledger():
    ledger = Ledger()
    mock = MockBackend(ledger)
    questions = {
        "a": {"type": "noul", "instructions": "Is this a personal purchase?"},
        "b": {"type": "noul", "instructions": "Is this a personal purchase?"},
        "s": {"type": "score", "instructions": "rate"},
    }
    out = mock.decide(state={"note": "personal gift", "description": "X"}, questions=questions, purpose="flags")
    assert out["a"]["noul"] == 0.9
    out2 = mock.decide(state={"note": "office toner"}, questions=questions, purpose="flags")
    assert out2["b"]["noul"] == 0.1
    assert out["s"]["type"] == "score" and out["s"]["confidence"] == 1.0
    assert ledger.calls["flags"] == 2


def test_mock_chat_normalize_and_review():
    mock = MockBackend(Ledger())
    user = json.dumps({"items": [
        {"key": "1", "description": "SQ *BLUE BOTTLE COFFEE #1234 OAKLAND CA", "note": ""},
        {"key": "2", "description": "WEIRD THING", "note": "", "reference_vendor": "Acme Corp"},
    ]})
    out = mock.chat_json(system="s", user=user, schema=NORMALIZE_SCHEMA, schema_name="n", purpose="normalize")
    assert out["results"][0]["vendor"] == "Blue Bottle Coffee"
    assert out["results"][1]["vendor"] == "Acme Corp"

    review_schema = {"type": "object", "required": ["reviews"], "properties": {"reviews": {"type": "array"}}}
    user = json.dumps({"groups": [
        {"group_id": "g1", "current_rule_id": "r1", "legal_options": [{"rule_id": "r1"}], "flags": []},
        {"group_id": "g2", "current_rule_id": "", "legal_options": [{"rule_id": "r2"}], "flags": []},
    ]})
    out = mock.chat_json(system="s", user=user, schema=review_schema, schema_name="r", purpose="review")
    assert [r["action"] for r in out["reviews"]] == ["keep", "unresolved"]


def test_validate_decision_rejections():
    q = {"c": {"type": "choice", "criteria": {"A": "a", "B": "b"}}, "n": {"type": "noul"}}
    good = {"c": {"type": "choice", "choice": "A"}, "n": {"type": "noul", "noul": 0.5}}
    assert validate_decision(good, q) == good
    bad_cases = [
        {"n": good["n"]},  # missing answer
        {**good, "c": {"type": "choice", "choice": "Z"}},  # not an option
        {**good, "n": {"type": "noul", "noul": 1.5}},  # out of range
        {**good, "n": {"type": "choice", "choice": "A"}},  # wrong type
    ]
    for bad in bad_cases:
        with pytest.raises(InvalidAnswer):
            validate_decision(bad, q)
    with pytest.raises(InvalidAnswer):
        validate_decision([], q)


# ------------------------------------------------------------ openrouter
def _chat_resp(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}],
            "usage": {"cost": 0.002, "prompt_tokens": 10, "completion_tokens": 5}}


def _backend(monkeypatch, responses: list[dict], ledger: Ledger | None = None):
    ledger = ledger or Ledger()
    be = OpenRouterBackend(LLMConfig(), ledger, api_key="test-key")
    calls: list[tuple[str, dict]] = []

    def fake_post(url, body):
        calls.append((url, body))
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setattr(be, "_post_json", fake_post)
    return be, calls, ledger


def test_openrouter_requires_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from spendcoder.llm.base import BackendError
    with pytest.raises(BackendError, match="OPENROUTER_API_KEY"):
        OpenRouterBackend(LLMConfig(), Ledger())


def test_openrouter_chat_json_fenced_and_ledger(monkeypatch):
    payload = '```json\n{"results": [{"key": "1", "vendor": "Acme"}]}\n```'
    be, calls, ledger = _backend(monkeypatch, [_chat_resp(payload)])
    out = be.chat_json(system="s", user="{}", schema=NORMALIZE_SCHEMA, schema_name="n", purpose="normalize")
    assert out == {"results": [{"key": "1", "vendor": "Acme"}]}
    url, body = calls[0]
    assert url.endswith("/v1/chat/completions")
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert ledger.calls["normalize"] == 1 and ledger.cost_usd == pytest.approx(0.002)
    assert ledger.input_tokens == 10 and ledger.output_tokens == 5


def test_openrouter_chat_json_invalid_then_valid(monkeypatch):
    good = '{"results": [{"key": "1", "vendor": "Acme"}]}'
    be, calls, _ = _backend(monkeypatch, [_chat_resp("not json"), _chat_resp('{"nope": 1}'), _chat_resp(good)])
    out = be.chat_json(system="s", user="{}", schema=NORMALIZE_SCHEMA, schema_name="n", purpose="normalize")
    assert out["results"][0]["vendor"] == "Acme" and len(calls) == 3


def test_openrouter_chat_json_gives_up(monkeypatch):
    be, calls, _ = _backend(monkeypatch, [_chat_resp("garbage")])
    with pytest.raises(InvalidAnswer):
        be.chat_json(system="s", user="{}", schema=NORMALIZE_SCHEMA, schema_name="n", purpose="normalize")
    assert len(calls) == 3


def test_openrouter_decide_retries_invalid(monkeypatch):
    questions = {"c": {"type": "choice", "instructions": "i", "criteria": {"A": "a", "B": "b"}}}
    bad = {"answers": {"c": {"type": "choice", "choice": "ZZ"}}, "usage": {"input_tokens": 3, "output_tokens": 1, "cost": 0.01}}
    good = {"answers": {"c": {"type": "choice", "choice": "B", "probabilities": {"A": 0.1, "B": 0.9}, "confidence": 0.9}},
            "usage": {"input_tokens": 3, "output_tokens": 1, "cost": 0.01}}
    be, calls, ledger = _backend(monkeypatch, [bad, good])
    out = be.decide(state={"description": "x"}, questions=questions, purpose="categorize")
    assert out["c"]["choice"] == "B"
    url, body = calls[0]
    assert url.endswith("/alpha/decisions")
    assert body == {"model": LLMConfig().decision_model, "state": {"description": "x"}, "questions": questions}
    assert ledger.calls["categorize"] == 2 and ledger.cost_usd == pytest.approx(0.02)


def test_openrouter_chat_text_and_models(monkeypatch):
    be, calls, _ = _backend(monkeypatch, [_chat_resp("hello")])
    assert be.chat_text(prompt="p", purpose="reflect") == "hello"
    assert "response_format" not in calls[0][1]
    monkeypatch.setattr(be, "_get_json", lambda url: {"data": [{"id": "m1"}]})
    assert be.list_decision_models() == [{"id": "m1"}]


# ----------------------------------------------------------------- cache
class _CountingBackend:
    name = "counting"

    def __init__(self):
        self.chat = self.dec = 0

    def chat_json(self, **kw):
        self.chat += 1
        return {"results": []}

    def decide(self, **kw):
        self.dec += 1
        return {"c": {"type": "noul", "noul": 0.2}}

    def chat_text(self, **kw):
        return "t"


def test_cached_backend_hit_skips_inner(tmp_path):
    ledger = Ledger()
    inner = _CountingBackend()
    be = CachedBackend(inner, tmp_path / "llm", ledger)
    assert be.name == "counting"
    kw = dict(system="s", user="u", schema=NORMALIZE_SCHEMA, schema_name="n", purpose="p")
    assert be.chat_json(**kw) == be.chat_json(**kw)
    assert inner.chat == 1 and ledger.cache_hits == 1
    be.chat_json(**{**kw, "user": "other"})
    assert inner.chat == 2
    dkw = dict(state={"a": 1}, questions={"c": {"type": "noul"}}, purpose="p")
    be.decide(**dkw)
    be.decide(**dkw)
    assert inner.dec == 1 and ledger.cache_hits == 2
    # a fresh wrapper over the same directory still hits
    CachedBackend(_CountingBackend(), tmp_path / "llm", ledger).decide(**dkw)
    assert ledger.cache_hits == 3


# ----------------------------------------------------------- agent files
def test_agent_files_roundtrip(tmp_path):
    work = tmp_path / "q"
    be = AgentFilesBackend(work, timeout_sec=20, ledger=Ledger(), poll_sec=0.05)
    assert (work / "README.md").exists()
    result: dict = {}

    def call():
        result["out"] = be.chat_json(system="s", user="{}", schema=NORMALIZE_SCHEMA, schema_name="n", purpose="normalize")

    t = threading.Thread(target=call)
    t.start()
    deadline = time.time() + 10
    rows: list[dict] = []
    while not rows and time.time() < deadline:
        rows = queue_list(work)
        time.sleep(0.02)
    assert rows and rows[0]["kind"] == "chat" and rows[0]["purpose"] == "normalize"
    rid = rows[0]["id"]
    assert rid.startswith("normalize-")
    assert queue_show(work, rid)["schema_name"] == "n"

    with pytest.raises(InvalidAnswer, match="invalid"):
        queue_answer(work, rid, {"wrong": 1})
    assert not (work / "responses" / f"{rid}.json").exists()

    queue_answer(work, rid, {"results": [{"key": "1", "vendor": "Acme"}]})
    t.join(timeout=10)
    assert result["out"] == {"results": [{"key": "1", "vendor": "Acme"}]}
    assert (work / "done" / f"{rid}.json").exists() and not queue_list(work)

    # resume: the stored response answers an identical request immediately
    again = be.chat_json(system="s", user="{}", schema=NORMALIZE_SCHEMA, schema_name="n", purpose="normalize")
    assert again == result["out"]


def test_agent_files_rejects_bad_hand_written_response(tmp_path):
    work = tmp_path / "q"
    be = AgentFilesBackend(work, timeout_sec=20, ledger=Ledger(), poll_sec=0.05)
    questions = {"c": {"type": "choice", "instructions": "i", "criteria": {"A": "a", "B": "b"}}}
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(out=be.decide(state={"x": 1}, questions=questions, purpose="cat")))
    t.start()
    deadline = time.time() + 10
    while not queue_list(work) and time.time() < deadline:
        time.sleep(0.02)
    rid = queue_list(work)[0]["id"]
    resp = work / "responses" / f"{rid}.json"
    resp.write_text(json.dumps({"id": rid, "output": {"c": {"type": "choice", "choice": "Z"}}}), encoding="utf-8")
    deadline = time.time() + 10
    while not list((work / "rejected").glob(f"{rid}.*.json")) and time.time() < deadline:
        time.sleep(0.02)
    rejected = list((work / "rejected").glob(f"{rid}.*.json"))
    assert rejected and "error" in json.loads(rejected[0].read_text(encoding="utf-8"))
    queue_answer(work, rid, {"c": {"type": "choice", "choice": "B"}})
    t.join(timeout=10)
    assert result["out"]["c"]["choice"] == "B"


def test_agent_files_timeout(tmp_path):
    from spendcoder.llm.base import BackendError
    be = AgentFilesBackend(tmp_path / "q", timeout_sec=0.2, ledger=Ledger(), poll_sec=0.05)
    with pytest.raises(BackendError, match="timed out"):
        be.chat_text(prompt="hi", purpose="reflect")


# ---------------------------------------------------------------- search
def test_parallel_search_maps_caches_and_fails_soft(tmp_path, monkeypatch):
    ledger = Ledger()
    s = ParallelSearch(SearchConfig(provider="parallel", max_chars_per_result=20), ledger, tmp_path, api_key="k")
    calls = []

    def fake_post(url, body):
        calls.append(body)
        return {"search_id": "x", "results": [
            {"url": "https://a.example", "title": "Acme", "excerpts": ["Acme sells anvils.", "More text here."]}]}

    monkeypatch.setattr(s, "_post_json", fake_post)
    out = s.lookup("ACME #12")
    assert out == [{"title": "Acme", "url": "https://a.example", "excerpt": "Acme sells anvils. M"}]
    assert "ACME #12" in calls[0]["objective"] and calls[0]["mode"] == "fast"
    assert s.lookup("ACME #12") == out and len(calls) == 1
    assert ledger.calls["search"] == 1

    def boom(url, body):
        raise RuntimeError("down")

    monkeypatch.setattr(s, "_post_json", boom)
    assert s.lookup("OTHER") == []
    assert ledger.calls["search_error"] == 1
    assert NullSearch().lookup("x") == []
