"""Onboarding: evolve prompt wording with GEPA against a labeled sample.

Flow: load + normalize labeled rows once -> split by merchant -> baseline -> GEPA ->
tuned eval on all splits -> promote (optionally) -> report. Only wording changes; the
policy is never touched (see ``adapter.SpendcoderAdapter``).
"""
from __future__ import annotations

import difflib
import json
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from spendcoder import pipeline, stages
from spendcoder.data import Txn, load_transactions
from spendcoder.pipeline import Context
from spendcoder.policy import descriptor_key
from spendcoder.prompts import TUNABLE, save_components
from spendcoder.tune.adapter import MockProposer, SpendcoderAdapter, run_pipeline

SPLITS = ("train", "val", "test")


class TuneError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
#  data
# --------------------------------------------------------------------------- #
def load_labeled(ctx: Context, normalize: bool = True) -> list[Txn]:
    """Labeled rows from ``dataset.labeled_path``; vendor-normalized unless ``normalize=False``."""
    path = ctx.cfg.dataset.labeled_path
    if path is None:
        raise TuneError("no labeled set configured: set [dataset] labeled_path in spendcoder.toml")
    _, txns = load_transactions(ctx.cfg.dataset, path)
    if not txns:
        raise TuneError(f"{path} has no rows")
    if normalize:
        stages.normalize(txns, ctx.policy, ctx.backend, ctx.comps, ctx.search, ctx.cfg, lambda *_: None)
    return txns


def evaluate_components(ctx: Context, txns: list[Txn], comps: dict[str, str] | None = None) -> dict:
    """Score ``comps`` (default: the project's) on normalized labeled ``txns``.

    Runs categorize -> match -> select on deep copies (review skipped); returns
    ``pipeline.summarize_scores``. Raises ValueError if the category guide is invalid.
    """
    return pipeline.summarize_scores(run_pipeline(ctx, txns, comps or ctx.comps))


def split_by_merchant(txns: list[Txn], split: tuple[float, float, float], seed: int
                      ) -> tuple[list[Txn], list[Txn], list[Txn]]:
    """Whole merchant groups (same ``descriptor_key``) go to one split; approximates ``split``."""
    groups: dict[str, list[Txn]] = defaultdict(list)
    for t in txns:
        groups[descriptor_key(t.description)].append(t)
    if len(groups) < 3:
        raise TuneError(f"need at least 3 distinct merchants in the labeled set to split it, found {len(groups)}; "
                        "add more labeled merchants")
    keys = sorted(groups)
    random.Random(seed).shuffle(keys)
    total = len(txns)
    target = [f * total for f in split]
    buckets: list[list[str]] = [[], [], []]
    sizes = [0, 0, 0]
    for k in keys:
        # largest remaining deficit relative to target wins; ties go to the earlier split
        i = max(range(3), key=lambda j: (target[j] - sizes[j]) / target[j] if target[j] else float("-inf"))
        buckets[i].append(k)
        sizes[i] += len(groups[k])
    for i in range(3):  # repair: every split gets at least one merchant
        while not buckets[i]:
            donor = max((j for j in range(3) if len(buckets[j]) > 1), key=lambda j: sizes[j])
            k = min(buckets[donor], key=lambda g: len(groups[g]))
            buckets[donor].remove(k)
            sizes[donor] -= len(groups[k])
            buckets[i].append(k)
            sizes[i] += len(groups[k])
    order = {t.id: n for n, t in enumerate(txns)}
    out = tuple(sorted((t for k in b for t in groups[k]), key=lambda t: order[t.id]) for b in buckets)
    return out  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
#  onboarding
# --------------------------------------------------------------------------- #
def _import_gepa():
    try:
        import gepa
    except ImportError as e:
        raise RuntimeError("GEPA is not installed: pip install 'spendcoder[tune]'") from e
    return gepa


def _new_run_dir(root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = root / f"onboard-{stamp}"
    n = 1
    while d.exists():
        n += 1
        d = root / f"onboard-{stamp}-{n}"
    d.mkdir(parents=True)
    return d


def onboard(ctx: Context, *, apply: bool = False, budget: int | None = None,
            components: list[str] | None = None, log: Callable[[str], None] = print) -> dict:
    cfg = ctx.cfg
    names = list(components or cfg.tune.components)
    bad = [c for c in names if c not in TUNABLE]
    if bad or not names:
        raise TuneError(f"unknown or empty component list {bad or names}; tunable: {', '.join(TUNABLE)}")
    gepa = _import_gepa()

    log(f"loading labeled set ({ctx.backend.name} backend)")
    txns = load_labeled(ctx)
    train, val, test = split_by_merchant(txns, cfg.tune.split, cfg.tune.seed)
    splits = {"train": train, "val": val, "test": test}
    merchants = {n: len({descriptor_key(t.description) for t in s}) for n, s in splits.items()}
    log("split by merchant: " + ", ".join(f"{n} {len(s)} rows / {merchants[n]} merchants" for n, s in splits.items()))

    base = dict(ctx.comps)
    baseline = {n: evaluate_components(ctx, s, base) for n, s in splits.items()}
    log("baseline: " + ", ".join(f"{n} {m['score']:.3f}" for n, m in baseline.items()))

    run_dir = _new_run_dir(cfg.runs_dir)
    max_calls = budget or cfg.tune.max_metric_calls
    adapter = SpendcoderAdapter(ctx, base)
    mock = ctx.backend.name == "mock"
    kwargs = {}
    if mock:
        kwargs["custom_candidate_proposer"] = MockProposer(ctx.policy)
    else:
        def reflection_lm(prompt):
            text = prompt if isinstance(prompt, str) else json.dumps(prompt)
            return ctx.backend.chat_text(prompt=text, purpose="reflect")
        kwargs["reflection_lm"] = reflection_lm

    class _Log:
        def log(self, message: str) -> None:
            text = " ".join(str(message).split())
            log("  gepa: " + (text if len(text) <= 160 else text[:157] + "..."))

    log(f"running GEPA on {', '.join(names)} (budget {max_calls} metric calls)")
    result = gepa.optimize(
        seed_candidate={k: base[k] for k in names},
        trainset=train, valset=val, adapter=adapter,
        frontier_type="instance",
        reflection_minibatch_size=cfg.tune.minibatch_size,
        max_metric_calls=max_calls,
        use_merge=False,
        cache_evaluation=False,
        seed=cfg.tune.seed,
        run_dir=str(run_dir / "gepa"),
        display_progress_bar=False,
        raise_on_exception=mock,
        logger=_Log(),
        **kwargs,
    )
    best = dict(result.best_candidate)
    log(f"GEPA finished: {result.num_candidates} candidates, {result.total_metric_calls} metric calls")

    tuned = {n: evaluate_components(ctx, s, {**base, **best}) for n, s in splits.items()}
    log("tuned:    " + ", ".join(f"{n} {m['score']:.3f}" for n, m in tuned.items()))

    changed = [k for k in names if best.get(k) != base.get(k)]
    if not apply:
        promoted, reason = False, "not promoted: --apply was not given (results kept in the run directory only)"
    elif not changed:
        promoted, reason = False, "not promoted: GEPA found nothing better than the current components"
    elif tuned["test"]["score"] < baseline["test"]["score"]:
        promoted, reason = False, (f"not promoted: held-out test score fell "
                                   f"({baseline['test']['score']:.3f} -> {tuned['test']['score']:.3f})")
    elif tuned["val"]["score"] < baseline["val"]["score"]:
        promoted, reason = False, (f"not promoted: validation score fell "
                                   f"({baseline['val']['score']:.3f} -> {tuned['val']['score']:.3f})")
    else:
        promoted, reason = True, (f"promoted: test {baseline['test']['score']:.3f} -> {tuned['test']['score']:.3f}, "
                                  f"val {baseline['val']['score']:.3f} -> {tuned['val']['score']:.3f}")

    new_comps = {**ctx.comps, **best}
    save_components(run_dir / "components.tuned.json", new_comps)
    if promoted:
        if cfg.components_path.exists():
            (run_dir / "components.previous.json").write_text(
                cfg.components_path.read_text(encoding="utf-8"), encoding="utf-8")
        save_components(cfg.components_path, new_comps)
        ctx.comps = new_comps
        log(f"wrote {cfg.components_path}")
    log(reason)

    lineage = []
    for i in range(result.num_candidates):
        parents = [p for p in result.parents[i] if p is not None]
        ref = result.candidates[parents[0]] if parents else {}
        lineage.append({
            "idx": i, "parents": parents,
            "val_score": round(float(result.val_aggregate_scores[i]), 4),
            "changed": [k for k in names if parents and result.candidates[i].get(k) != ref.get(k)],
        })
    summary = {
        "run_dir": str(run_dir), "backend": ctx.backend.name, "components": names, "budget": max_calls,
        "split_rows": {n: len(s) for n, s in splits.items()}, "split_merchants": merchants,
        "baseline": baseline, "tuned": tuned, "promoted": promoted, "reason": reason,
        "best_idx": result.best_idx, "num_candidates": result.num_candidates,
        "parents": [list(p) for p in result.parents],
        "val_aggregate_scores": [round(float(s), 4) for s in result.val_aggregate_scores],
        "metric_calls": result.total_metric_calls, "usage": ctx.ledger.to_dict(),
    }
    (run_dir / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    report_path = run_dir / "report.md"
    report_path.write_text(_report(ctx, summary, base, best, lineage), encoding="utf-8")
    log(f"report: {report_path}")
    return {
        "run_dir": run_dir, "report_path": report_path, "baseline": baseline, "tuned": tuned,
        "promoted": promoted, "reason": reason, "metric_calls": result.total_metric_calls,
        "num_candidates": result.num_candidates,
    }


# --------------------------------------------------------------------------- #
#  report
# --------------------------------------------------------------------------- #
def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _report(ctx: Context, s: dict, base: dict[str, str], best: dict[str, str], lineage: list[dict]) -> str:
    b, t = s["baseline"], s["tuned"]
    L = [
        f"# Onboarding report: {ctx.cfg.name}",
        "",
        f"- Run: `{Path(s['run_dir']).name}` | backend `{s['backend']}` | components: {', '.join(s['components'])}",
        f"- Budget: {s['budget']} metric calls (used {s['metric_calls']}), "
        f"{s['num_candidates']} candidates, best candidate #{s['best_idx']}",
        f"- Seed {ctx.cfg.tune.seed}, split {tuple(ctx.cfg.tune.split)}; review stage skipped during tuning",
        "",
        "## Split",
        "",
        "| split | rows | merchants |",
        "|---|---|---|",
    ]
    L += [f"| {n} | {s['split_rows'][n]} | {s['split_merchants'][n]} |" for n in SPLITS]
    L += ["", "## Scores", "",
          "| split | baseline score | tuned score | baseline category | tuned category | baseline GL | tuned GL |",
          "|---|---|---|---|---|---|---|"]
    for n in SPLITS:
        L.append(f"| {n} | {b[n]['score']:.3f} | {t[n]['score']:.3f} | {_pct(b[n]['category_accuracy'])} | "
                 f"{_pct(t[n]['category_accuracy'])} | {_pct(b[n]['gl_accuracy'])} | {_pct(t[n]['gl_accuracy'])} |")
    L += ["", "Look at the test row first; the test split is never used to choose a candidate.", "",
          "## Decision", "", f"**{'Promoted' if s['promoted'] else 'Not promoted'}.** {s['reason']}", "",
          "## Candidate lineage", "", "| candidate | parent | val score | changed |", "|---|---|---|---|"]
    for r in lineage:
        parent = ", ".join(f"#{p}" for p in r["parents"]) or "seed"
        L.append(f"| #{r['idx']}{' (best)' if r['idx'] == s['best_idx'] else ''} | {parent} | "
                 f"{r['val_score']:.3f} | {', '.join(r['changed']) or '-'} |")
    L += ["", "## Component changes", ""]
    any_diff = False
    for k in s["components"]:
        if best.get(k) == base.get(k):
            continue
        any_diff = True
        diff = difflib.unified_diff(base[k].splitlines(), best[k].splitlines(),
                                    fromfile=f"{k} (baseline)", tofile=f"{k} (tuned)", lineterm="", n=1)
        L += [f"### `{k}`", "", "```diff", *diff, "```", ""]
    if not any_diff:
        L += ["No component changed.", ""]
    L += ["## Run metadata", "", f"- Usage: `{json.dumps(s['usage'])}`", ""]
    return "\n".join(L)
