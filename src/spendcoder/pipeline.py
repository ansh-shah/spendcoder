"""Orchestration: open a project, code transactions, write a run directory."""
from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from spendcoder import __version__, stages
from spendcoder.config import Config, load_config
from spendcoder.data import Txn, load_transactions, write_output
from spendcoder.llm.base import Backend, Ledger
from spendcoder.llm.factory import make_backend
from spendcoder.policy import Policy, load_policy
from spendcoder.prompts import load_components
from spendcoder.search import make_search


@dataclass
class Context:
    cfg: Config
    policy: Policy
    backend: Backend
    search: object
    comps: dict[str, str]
    ledger: Ledger


def open_project(project: str | Path = ".", backend: str | None = None) -> Context:
    cfg = load_config(project)
    policy = load_policy(cfg.policy)
    ledger = Ledger()
    be = make_backend(cfg, ledger, backend)
    search = make_search(cfg, ledger) if (backend or cfg.llm.backend) != "mock" else _no_search()
    comps = load_components(cfg.components_path, policy.categories)
    return Context(cfg, policy, be, search, comps, ledger)


def _no_search():
    from spendcoder.search import NullSearch
    return NullSearch()


def code(txns: list[Txn], ctx: Context, comps: dict[str, str] | None = None, *,
         review: bool = True, log: Callable[[str], None] = print) -> None:
    """normalize -> categorize -> match -> select -> review, in place."""
    comps = comps or ctx.comps
    cfg, w = ctx.cfg, ctx.cfg.llm.max_concurrency
    steps = [
        ("normalize vendors", lambda: stages.normalize(txns, ctx.policy, ctx.backend, comps, ctx.search, cfg, log)),
        ("categorize", lambda: stages.categorize(txns, ctx.policy, ctx.backend, comps, cfg.gates, w)),
        ("match rules", lambda: stages.match(txns, ctx.policy)),
        ("select rule", lambda: stages.select(txns, ctx.policy, ctx.backend, comps, cfg.gates, w)),
    ]
    if review:
        steps.append(("review", lambda: stages.review(txns, ctx.policy, ctx.backend, comps, cfg, log)))
    for name, fn in steps:
        t0 = time.time()
        log(f"> {name}")
        fn()
        log(f"  done in {time.time() - t0:.1f}s")
    if not review:
        for t in txns:
            stages._finalize(t)


# --------------------------------------------------------------------------- #
#  scoring against a labeled set
# --------------------------------------------------------------------------- #
def labels(t: Txn) -> tuple[str, str]:
    return t.raw.get("expected_category", "").strip(), t.raw.get("expected_gl_code", "").strip()


def score_txn(t: Txn) -> tuple[float, str]:
    """0.5 for the right category + 0.5 for the right final GL code, with feedback text."""
    exp_cat, exp_gl = labels(t)
    parts, notes = [], []
    if exp_cat:
        ok = t.category.lower() == exp_cat.lower()
        parts.append(1.0 if ok else 0.0)
        if not ok:
            got = t.category or f"Unknown ({t.category_method or 'no answer'})"
            notes.append(f"category: expected {exp_cat!r}, got {got!r}")
    if exp_gl:
        got_gl = t.final_gl_code or t.gl_code
        ok = got_gl == exp_gl
        parts.append(1.0 if ok else 0.0)
        if not ok:
            notes.append(f"GL: expected {exp_gl}, got {got_gl or 'none'} via {t.select_method or 'no rule'}"
                         f" (eligible: {', '.join(t.candidates) or 'none'})")
    score = sum(parts) / len(parts) if parts else 0.0
    return score, "; ".join(notes) or "correct"


def summarize_scores(txns: list[Txn]) -> dict:
    n = len(txns)
    cat_rows = [t for t in txns if labels(t)[0]]
    gl_rows = [t for t in txns if labels(t)[1]]
    cat_ok = sum(t.category.lower() == labels(t)[0].lower() for t in cat_rows)
    gl_ok = sum((t.final_gl_code or t.gl_code) == labels(t)[1] for t in gl_rows)
    return {
        "rows": n,
        "score": round(sum(score_txn(t)[0] for t in txns) / n, 4) if n else 0.0,
        "category_accuracy": round(cat_ok / len(cat_rows), 4) if cat_rows else None,
        "gl_accuracy": round(gl_ok / len(gl_rows), 4) if gl_rows else None,
    }


# --------------------------------------------------------------------------- #
#  run directory
# --------------------------------------------------------------------------- #
def _sha(path: Path | None) -> str | None:
    if not path or not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(ctx: Context, input_path: Path | None = None, run_id: str | None = None,
        limit: int | None = None, log: Callable[[str], None] = print) -> Path:
    cfg = ctx.cfg
    src = input_path or cfg.dataset.path
    header, txns = load_transactions(cfg.dataset, src)
    if limit:
        txns = txns[:limit]
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    out = cfg.runs_dir / run_id
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"run directory {out} already exists; pick a new --run-id")
    out.mkdir(parents=True, exist_ok=True)
    log(f"spendcoder {__version__} | {len(txns)} transactions | backend={ctx.backend.name} | run {run_id}")

    started = time.time()
    code(txns, ctx, log=log)
    ids_in = [t.id for t in txns]

    write_output(out / "coded.csv", header, txns)
    queue = [t for t in txns if t.review_status == "unresolved" or not t.final_gl_code]
    with (out / "review_queue.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "description", "note", "amount", "department", "vendor", "category",
                    "candidate_rules", "suggested_rule", "flags", "reason"])
        for t in queue:
            w.writerow([t.id, t.description, t.note, f"{t.amount:.2f}", t.department, t.vendor, t.category,
                        "|".join(t.candidates), t.rule_id, "|".join(t.flags),
                        t.review_reason or t.select_reason or "no GL assigned"])
    with (out / "audit.jsonl").open("w", encoding="utf-8") as f:
        for t in txns:
            f.write(json.dumps({
                "id": t.id,
                "vendor": {"value": t.vendor, "method": t.vendor_method,
                           "web_evidence": [e.get("url") for e in t.web_evidence]},
                "category": {"value": t.category, "method": t.category_method, "confidence": t.category_confidence},
                "eligible_rules": t.candidates,
                "selection": {"rule_id": t.rule_id, "method": t.select_method,
                              "confidence": t.select_confidence, "reason": t.select_reason},
                "review": {"flags": t.flags, "status": t.review_status, "reason": t.review_reason},
                "final": {"rule_id": t.final_rule_id, "gl_code": t.final_gl_code, "gl_name": t.final_gl_name},
            }, ensure_ascii=False) + "\n")

    # Invariant: same rows, same order, nothing invented.
    assert ids_in == [t.id for t in txns], "row order changed"
    for t in txns:
        if t.final_rule_id:
            r = ctx.policy.by_id[t.final_rule_id]
            assert t.final_rule_id in t.candidates, f"{t.id}: final rule not eligible"
            assert (t.final_gl_code, t.final_gl_name) == (r.gl_code, r.gl_name), f"{t.id}: GL pair mismatch"

    stats = {
        "rows": len(txns),
        "coded": sum(bool(t.final_gl_code) for t in txns),
        "needs_review": len(queue),
        "category_methods": dict(Counter(t.category_method for t in txns)),
        "select_methods": dict(Counter(t.select_method or "n/a" for t in txns)),
        "review_status": dict(Counter(t.review_status or "not flagged" for t in txns)),
        "flags": dict(Counter(f for t in txns for f in t.flags)),
    }
    if any(labels(t)[0] or labels(t)[1] for t in txns):
        stats["labeled_eval"] = summarize_scores(txns)
    manifest = {
        "spendcoder_version": __version__,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_sec": round(time.time() - started, 1),
        "backend": ctx.backend.name,
        "models": {"chat": cfg.llm.chat_model, "decision": cfg.llm.decision_model},
        "search": ctx.search.name,
        "inputs": {"transactions": {"path": str(src), "sha256": _sha(src)}},
        "policy": {k: _sha(getattr(cfg.policy, k)) for k in ("categories", "rules", "vendor_overrides", "vendors")},
        "prompts_sha256": hashlib.sha256(json.dumps(ctx.comps, sort_keys=True).encode()).hexdigest(),
        "usage": ctx.ledger.to_dict(),
        "stats": stats,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(_summary_md(manifest), encoding="utf-8")
    log(f"\ncoded {stats['coded']}/{stats['rows']} | {stats['needs_review']} in review queue | "
        f"cost ${ctx.ledger.cost_usd:.4f} | {out}")
    return out


def _summary_md(m: dict) -> str:
    s = m["stats"]
    pct = 100 * s["coded"] / s["rows"] if s["rows"] else 0.0
    lines = [
        f"# spendcoder run `{m['run_id']}`",
        "",
        f"- Rows: **{s['rows']}**, coded: **{s['coded']}** ({pct:.1f}%), review queue: **{s['needs_review']}**",
        f"- Backend: `{m['backend']}` (decision `{m['models']['decision']}`, chat `{m['models']['chat']}`), "
        f"search: `{m['search']}`",
        f"- Model calls: {sum(m['usage']['calls'].values())} ({m['usage']['cache_hits']} cache hits), "
        f"cost ${m['usage']['cost_usd']:.4f}, {m['duration_sec']}s",
        "",
    ]
    for title, key in (("Category method", "category_methods"), ("Rule selection", "select_methods"),
                       ("Review", "review_status"), ("Flags", "flags")):
        if s.get(key):
            lines += [f"## {title}", "", "| value | rows |", "|---|---|"]
            lines += [f"| {k or '(blank)'} | {v} |" for k, v in sorted(s[key].items(), key=lambda kv: -kv[1])]
            lines.append("")
    if "labeled_eval" in s:
        e = s["labeled_eval"]
        lines += ["## Labeled evaluation", "", f"- score {e['score']:.3f}, category accuracy "
                  f"{e['category_accuracy']}, GL accuracy {e['gl_accuracy']}", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  doctor
# --------------------------------------------------------------------------- #
def doctor(cfg: Config) -> tuple[list[str], list[str]]:
    """Return (problems, notes). Problems block a run; notes are worth reading."""
    problems, notes = [], []
    try:
        policy = load_policy(cfg.policy)
    except Exception as e:  # noqa: BLE001 - surface any policy error as text
        return [f"policy: {e}"], notes
    notes.append(f"policy: {len(policy.categories)} categories, {len(policy.rules)} rules, "
                 f"{len(policy.vendor_overrides)} vendor overrides, {len(policy.vendors)} reference vendors")
    ruled = {r.category for r in policy.rules}
    orphan = [c for c in policy.categories if c not in ruled]
    if orphan:
        notes.append(f"categories with no rule (rows land in review): {', '.join(orphan)}")
    dup_scope = Counter((r.category, r.department, r.amount_min, r.amount_max) for r in policy.rules if not r.keywords)
    clashes = [k for k, v in dup_scope.items() if v > 1]
    if clashes:
        notes.append(f"{len(clashes)} scope(s) have several no-keyword default rules; those rows need the decision model")
    try:
        load_components(cfg.components_path, policy.categories)
    except ValueError as e:
        problems.append(f"prompts: {e} (delete {cfg.components_path} or re-run onboard)")
    for label, path in (("dataset", cfg.dataset.path), ("labeled set", cfg.dataset.labeled_path)):
        if path is None:
            notes.append("no labeled set configured: onboarding (GEPA) needs one")
            continue
        try:
            _, txns = load_transactions(cfg.dataset, path)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{label}: {e}")
            continue
        depts = Counter(t.department for t in txns)
        notes.append(f"{label}: {len(txns)} rows, {len(depts)} departments, "
                     f"{sum(bool(t.note) for t in txns)} with notes")
        unknown_depts = {d for d in depts if d} - {r.department for r in policy.rules if r.department}
        if label == "dataset" and unknown_depts and any(r.department for r in policy.rules):
            notes.append(f"departments never named by a rule (only department-agnostic rules apply): "
                         f"{', '.join(sorted(unknown_depts))}")
        if label == "labeled set":
            missing = sum(not (labels(t)[0] or labels(t)[1]) for t in txns)
            if missing:
                problems.append(f"labeled set: {missing} rows have neither expected_category nor expected_gl_code")
            bad = [t.id for t in txns if labels(t)[0] and labels(t)[0] not in policy.categories]
            if bad:
                problems.append(f"labeled set: unknown expected_category on {bad[:5]}")
    if cfg.llm.backend == "openrouter":
        import os
        from spendcoder.envfile import load_dotenv
        load_dotenv(cfg.root)
        if not os.environ.get("OPENROUTER_API_KEY"):
            problems.append("OPENROUTER_API_KEY is not set (env or .env); use --backend mock to try offline")
        if cfg.search.provider == "parallel" and not os.environ.get("PARALLEL_API_KEY"):
            notes.append("PARALLEL_API_KEY not set: web evidence for unknown merchants is disabled")
    return problems, notes
