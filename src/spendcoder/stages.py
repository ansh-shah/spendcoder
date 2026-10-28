"""Pipeline stages. Each stage mutates the list of ``Txn`` in place.

normalize  -> chat (strict JSON) + optional Parallel web evidence
categorize -> Decisions `choice` over your categories
match      -> deterministic rule eligibility
select     -> literal keywords in code, then Decisions `choice` over eligible rules
review     -> detectors + Decisions `noul` flags, then a chat reviewer limited to legal rules
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from spendcoder.config import Config, GateConfig, ReviewConfig
from spendcoder.data import Txn
from spendcoder.llm.base import Backend, BackendError, InvalidAnswer, choice_confidence
from spendcoder.policy import Policy, Rule, descriptor_key, match_reference_vendor
from spendcoder.prompts import UNKNOWN, parse_category_guide


class StageError(RuntimeError):
    pass


def _pmap(fn: Callable, items: list, workers: int) -> list:
    if workers <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def _groups(txns: Iterable[Txn], key: Callable[[Txn], tuple]) -> dict[tuple, list[Txn]]:
    out: dict[tuple, list[Txn]] = defaultdict(list)
    for t in txns:
        out[key(t)].append(t)
    return dict(out)


def _gate_errors(stage: str, failed: int, total: int, gates: GateConfig) -> None:
    if total and failed / total > gates.max_error_rate:
        raise StageError(
            f"{stage}: {failed}/{total} calls failed (> max_error_rate {gates.max_error_rate:.0%}); "
            "refusing to produce a mostly-empty result. Check the backend and rerun (answers are cached)."
        )


# --------------------------------------------------------------------------- #
#  normalize
# --------------------------------------------------------------------------- #
NORMALIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "vendor": {"type": "string"}},
                "required": ["key", "vendor"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def normalize(txns: list[Txn], policy: Policy, backend: Backend, comps: dict[str, str], search,
              cfg: Config, log=print) -> None:
    groups = _groups(txns, lambda t: (t.description, t.note))
    items = []
    for i, ((desc, note), members) in enumerate(groups.items()):
        ref = match_reference_vendor(desc, policy.vendors)
        item = {"key": f"g{i}", "description": desc, "note": note}
        if ref:
            item["reference_vendor"] = ref
        items.append((item, members))

    # Web evidence: one lookup per descriptor family, only for unknown merchants by default.
    needs = {}
    for item, _ in items:
        if cfg.search.only_unknown_merchants and "reference_vendor" in item:
            continue
        needs.setdefault(descriptor_key(item["description"]), item["description"])
    if needs and search.name != "none":
        log(f"  web search: {len(needs)} descriptor families via {search.name}")
        found = dict(zip(needs, _pmap(lambda d: search.lookup(d), list(needs.values()), cfg.llm.max_concurrency)))
        for item, members in items:
            ev = found.get(descriptor_key(item["description"]))
            if ev:
                item["web_evidence"] = ev
                for t in members:
                    t.web_evidence = ev

    system = comps["normalize_system"]
    if policy.note_glossary:
        system += "\nNote shorthand used by cardholders:\n" + "\n".join(
            f"- {k}: {v}" for k, v in policy.note_glossary.items())
    size = max(1, cfg.llm.normalize_batch_size)
    batches = [items[i:i + size] for i in range(0, len(items), size)]

    def run(batch):
        payload = {"items": [it for it, _ in batch]}
        try:
            out = backend.chat_json(system=system, user=json.dumps(payload, ensure_ascii=False),
                                    schema=NORMALIZE_SCHEMA, schema_name="vendor_normalization",
                                    purpose="normalize")
            return {r["key"]: r["vendor"].strip() for r in out["results"]}
        except (BackendError, InvalidAnswer) as e:
            log(f"  normalize batch failed: {e}")
            return None

    failed = 0
    for batch, result in zip(batches, _pmap(run, batches, cfg.llm.max_concurrency)):
        if result is None:
            failed += 1
        for item, members in batch:
            name = (result or {}).get(item["key"], "")
            for t in members:
                if name:
                    t.vendor, t.vendor_method = name, "model"
                else:
                    t.vendor = item.get("reference_vendor", "") or t.description
                    t.vendor_method = "fallback"
    _gate_errors("normalize", failed, len(batches), cfg.gates)


# --------------------------------------------------------------------------- #
#  categorize
# --------------------------------------------------------------------------- #
def categorize(txns: list[Txn], policy: Policy, backend: Backend, comps: dict[str, str],
               gates: GateConfig, workers: int = 8) -> None:
    criteria = parse_category_guide(comps["category_guide"], policy.categories)
    overrides = {k.lower(): v for k, v in policy.vendor_overrides.items()}
    todo: list[Txn] = []
    for t in txns:
        forced = overrides.get(t.vendor.lower())
        if forced:
            t.category, t.category_method, t.category_confidence = forced, "override", 1.0
        else:
            todo.append(t)

    groups = _groups(todo, lambda t: (t.description, t.note, t.vendor))
    question = {"category": {"type": "choice", "instructions": comps["categorize_instructions"],
                             "criteria": criteria}}

    def run(members: list[Txn]):
        t = members[0]
        state = {k: v for k, v in t.evidence_view().items() if k not in ("category", "department", "amount")}
        try:
            return backend.decide(state=state, questions=question, purpose="categorize")["category"]
        except (BackendError, InvalidAnswer):
            return None

    member_lists = list(groups.values())
    failed = 0
    for members, ans in zip(member_lists, _pmap(run, member_lists, workers)):
        for t in members:
            if ans is None:
                t.category, t.category_method = "", "error"
                continue
            conf = choice_confidence(ans)
            t.category_confidence = conf
            if ans["choice"] == UNKNOWN:
                t.category, t.category_method = "", "decision"
            elif conf < gates.category_min_confidence:
                t.category, t.category_method = "", "gated"
            else:
                t.category, t.category_method = ans["choice"], "decision"
        failed += ans is None
    _gate_errors("categorize", failed, len(member_lists), gates)


# --------------------------------------------------------------------------- #
#  match (deterministic)
# --------------------------------------------------------------------------- #
def _assign(t: Txn, r: Rule, method: str, conf: float | None, reason: str) -> None:
    t.rule_id, t.gl_code, t.gl_name = r.rule_id, r.gl_code, r.gl_name
    t.select_method, t.select_confidence, t.select_reason = method, conf, reason


def match(txns: list[Txn], policy: Policy) -> None:
    for t in txns:
        t.rule_id = t.gl_code = t.gl_name = ""
        rules = policy.eligible(t.category, t.department, t.amount) if t.category else []
        t.candidates = [r.rule_id for r in rules]
        if len(rules) == 1:
            _assign(t, rules[0], "unique", 1.0, "only eligible rule")
        elif not rules:
            t.select_method = "none"


# --------------------------------------------------------------------------- #
#  select
# --------------------------------------------------------------------------- #
def keyword_hits(rule: Rule, text: str, aliases: dict[str, list[str]]) -> list[str]:
    """Literal, case-insensitive keyword matching. Aliases are explicit expansions
    from config (e.g. "happy hour" -> HH); there is no conceptual matching here."""
    low = text.lower()
    hits = []
    for kw in rule.keywords:
        forms = [kw] + aliases.get(kw.lower(), [])
        for form in forms:
            f = form.lower()
            pattern = r"\b" + re.escape(f) + r"\b" if len(f) <= 3 else re.escape(f)
            if re.search(pattern, low):
                hits.append(kw)
                break
    return hits


def select(txns: list[Txn], policy: Policy, backend: Backend, comps: dict[str, str],
           gates: GateConfig, workers: int = 8) -> None:
    pending: list[Txn] = []
    for t in txns:
        if len(t.candidates) < 2:
            continue
        rules = [policy.by_id[r] for r in t.candidates]
        hits = {r.rule_id: keyword_hits(r, t.note, policy.keyword_aliases) for r in rules}
        kw = [r for r in rules if hits[r.rule_id]]
        if len(kw) == 1:
            _assign(t, kw[0], "keyword", 1.0, f"note contains {', '.join(hits[kw[0].rule_id])!r}")
            continue
        defaults = [r for r in rules if not r.keywords]
        if not kw and len(defaults) == 1:
            _assign(t, defaults[0], "default", 1.0, "no keyword matched; single default rule")
            continue
        pending.append(t)

    groups = _groups(pending, lambda t: (t.vendor, t.category, t.note, tuple(t.candidates)))

    def run(members: list[Txn]):
        t = members[0]
        options = {r: policy.by_id[r].describe() for r in t.candidates}
        q = {"rule": {"type": "choice", "instructions": comps["select_instructions"], "criteria": options}}
        try:
            return backend.decide(state=t.evidence_view(), questions=q, purpose="select")["rule"]
        except (BackendError, InvalidAnswer):
            return None

    member_lists = list(groups.values())
    failed = 0
    for members, ans in zip(member_lists, _pmap(run, member_lists, workers)):
        failed += ans is None
        for t in members:
            if ans is None:
                t.select_method = "error"
                continue
            conf = choice_confidence(ans)
            if conf < gates.select_min_confidence:
                t.select_method, t.select_confidence = "gated", conf
                t.select_reason = f"best option {ans['choice']} below confidence gate"
                continue
            _assign(t, policy.by_id[ans["choice"]], "decision", conf, "decision model among eligible rules")
    _gate_errors("select", failed, len(member_lists), gates)


# --------------------------------------------------------------------------- #
#  review
# --------------------------------------------------------------------------- #
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["keep", "change", "unresolved"]},
                    "rule_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["group_id", "action", "rule_id", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["reviews"],
    "additionalProperties": False,
}


def _finalize(t: Txn) -> None:
    t.final_rule_id, t.final_gl_code, t.final_gl_name = t.rule_id, t.gl_code, t.gl_name


def detect(txns: list[Txn], policy: Policy, backend: Backend, rc: ReviewConfig, workers: int = 8) -> None:
    """Deterministic detectors, then configured yes/no flags (Decisions `noul`)."""
    gl_names = sorted({r.gl_name for r in policy.rules if len(r.gl_name) >= 6}, key=len, reverse=True)
    for t in txns:
        f = []
        if not t.category:
            f.append("unknown_category")
        elif not t.candidates:
            f.append("no_eligible_rule")
        if t.select_method in ("gated", "error"):
            f.append("unselected")
        if t.select_confidence is not None and t.select_confidence < rc.low_confidence:
            f.append("low_confidence")
        note = t.note.lower()
        if note and t.gl_name and any(n.lower() in note and n != t.gl_name for n in gl_names):
            f.append("note_names_other_gl")
        t.flags = f

    if not rc.flags:
        return
    questions = {fl.name: {"type": "noul", "instructions": fl.question} for fl in rc.flags}
    with_note = _groups([t for t in txns if t.note], lambda t: (t.note, t.vendor))

    def run(members: list[Txn]):
        t = members[0]
        state = {"note": t.note, "vendor": t.vendor, "description": t.description}
        try:
            return backend.decide(state=state, questions=questions, purpose="review_flags")
        except (BackendError, InvalidAnswer):
            return None

    member_lists = list(with_note.values())
    for members, ans in zip(member_lists, _pmap(run, member_lists, workers)):
        for t in members:
            if ans is None:
                t.flags.append("flag_check_failed")
                continue
            for fl in rc.flags:
                if ans[fl.name]["noul"] >= fl.threshold:
                    t.flags.append(fl.name)


def review(txns: list[Txn], policy: Policy, backend: Backend, comps: dict[str, str], cfg: Config,
           log=print) -> None:
    rc = cfg.review
    for t in txns:
        t.review_status = t.review_reason = ""
        _finalize(t)
    if not rc.enabled:
        return
    detect(txns, policy, backend, rc, cfg.llm.max_concurrency)
    hold = {f.name for f in rc.flags if f.action == "hold"}
    flagged = [t for t in txns if t.flags]

    reviewable = []
    for t in flagged:
        held = hold.intersection(t.flags)
        if held:
            t.review_status = "unresolved"
            t.review_reason = f"held for a person: {', '.join(sorted(held))}"
            t.final_rule_id = t.final_gl_code = t.final_gl_name = ""
        elif not t.candidates:
            t.review_status = "unresolved"
            t.review_reason = "no eligible rule; needs a policy decision"
        else:
            reviewable.append(t)

    # Grouping on the candidate set means every row in a group has identical legal options,
    # so a group-level answer is legal for every row (no group-union loophole).
    groups = list(_groups(reviewable, lambda t: (t.vendor, t.category, t.note, t.description,
                                                 tuple(t.candidates))).values())
    if not groups:
        return
    payload_groups, index = [], {}
    for i, members in enumerate(groups):
        t = members[0]
        gid = f"g{i:04d}"
        index[gid] = members
        payload_groups.append({
            "group_id": gid,
            "transaction_count": len(members),
            "vendor": t.vendor, "category": t.category, "note": t.note, "description": t.description,
            "total_abs_amount": round(sum(abs(m.amount) for m in members), 2),
            "current_rule_id": t.rule_id,
            "flags": sorted({f for m in members for f in m.flags}),
            "legal_options": [{"rule_id": r, "gl_code": policy.by_id[r].gl_code, "gl_name": policy.by_id[r].gl_name,
                               "keywords": list(policy.by_id[r].keywords)} for r in t.candidates],
        })

    answers: dict[str, dict] = {}
    batches = [payload_groups[i:i + 25] for i in range(0, len(payload_groups), 25)]

    def run(batch):
        try:
            out = backend.chat_json(system=comps["review_system"], user=json.dumps({"groups": batch}),
                                    schema=REVIEW_SCHEMA, schema_name="review", purpose="review")
            return out["reviews"]
        except (BackendError, InvalidAnswer) as e:
            log(f"  review batch failed: {e}")
            return []

    for result in _pmap(run, batches, cfg.llm.max_concurrency):
        for r in result:
            if r["group_id"] in index:
                answers[r["group_id"]] = r

    for gid, members in index.items():
        r = answers.get(gid)
        legal = set(members[0].candidates)
        for t in members:
            if r is None:
                t.review_status, t.review_reason = "unresolved", "reviewer gave no answer"
            elif r["action"] == "unresolved":
                t.review_status, t.review_reason = "unresolved", r["reason"]
            elif r["action"] == "keep" and t.rule_id:
                t.review_status, t.review_reason = "kept", r["reason"]
            elif r["action"] == "change" and r["rule_id"] in legal:
                rule = policy.by_id[r["rule_id"]]
                t.final_rule_id, t.final_gl_code, t.final_gl_name = rule.rule_id, rule.gl_code, rule.gl_name
                t.review_status = "changed" if rule.rule_id != t.rule_id else "kept"
                t.review_reason = r["reason"]
            else:
                t.review_status = "unresolved"
                t.review_reason = f"reviewer answer rejected (illegal rule {r['rule_id']!r} or keep without a rule)"
            if t.review_status == "unresolved":
                t.final_rule_id = t.final_gl_code = t.final_gl_name = ""
