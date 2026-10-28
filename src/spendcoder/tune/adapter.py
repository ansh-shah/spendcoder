"""GEPA adapter: scores prompt components by running the real pipeline on labeled rows.

Only wording is evaluated. Categories, rules, thresholds and GL pairs come from the
policy and config, never from a candidate. A candidate whose ``category_guide`` does
not list every category exactly once scores 0.0 on every example (and says why in the
trajectory) instead of raising, so the search learns to avoid that lineage.
"""
from __future__ import annotations

import copy
import random
import re
from typing import Any

from spendcoder import pipeline, stages
from spendcoder.data import Txn
from spendcoder.pipeline import Context
from spendcoder.prompts import TUNABLE, parse_category_guide

GUIDE_FORMAT_NOTE = (
    "Format rule for category_guide: write exactly one `Category: description` line per category. "
    "Every existing category must remain exactly once and no category may be added, renamed or removed. "
    "Only rewrite the descriptions."
)
MAX_FAILURES_PER_COMPONENT = 12
MAX_SUCCESSES_PER_COMPONENT = 2


def run_pipeline(ctx: Context, txns: list[Txn], comps: dict[str, str]) -> list[Txn]:
    """categorize -> match -> select -> finalize on deep copies (review is skipped).

    ``txns`` must already be normalized. The inputs are never mutated.
    """
    work = copy.deepcopy(txns)
    cfg, w = ctx.cfg, ctx.cfg.llm.max_concurrency
    stages.categorize(work, ctx.policy, ctx.backend, comps, cfg.gates, w)
    stages.match(work, ctx.policy)
    stages.select(work, ctx.policy, ctx.backend, comps, cfg.gates, w)
    for t in work:
        stages._finalize(t)
    return work


def candidate_error(ctx: Context, candidate: dict[str, str], base_comps: dict[str, str]) -> str:
    """Why a candidate may not be scored, or "" if it is valid."""
    extra = sorted(set(candidate) - set(TUNABLE))
    if extra:
        return f"candidate contains components that are not tunable: {extra}"
    guide = candidate.get("category_guide", base_comps.get("category_guide", ""))
    try:
        parse_category_guide(guide, ctx.policy.categories)
    except ValueError as e:
        return f"invalid category_guide: {e}"
    return ""


def _inputs(t: Txn) -> dict[str, Any]:
    view = {k: v for k, v in t.evidence_view().items() if k != "category"}
    return view


class SpendcoderAdapter:
    """``gepa.core.adapter.GEPAAdapter`` over precomputed (normalized) labeled ``Txn`` rows."""

    propose_new_texts = None

    def __init__(self, ctx: Context, base_comps: dict[str, str] | None = None, seed: int | None = None) -> None:
        self.ctx = ctx
        self.base_comps = dict(base_comps if base_comps is not None else ctx.comps)
        self.seed = ctx.cfg.tune.seed if seed is None else seed
        self.metric_calls = 0

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, batch: list[Txn], candidate: dict[str, str], capture_traces: bool = False):
        from gepa.core.adapter import EvaluationBatch

        self.metric_calls += len(batch)
        error = candidate_error(self.ctx, candidate, self.base_comps)
        if error:
            outputs = [{"error": error} for _ in batch]
            scores = [0.0] * len(batch)
            trajs = None
            if capture_traces:
                trajs = [{
                    "inputs": _inputs(t), "outputs": {"error": error}, "expected": self._expected(t),
                    "score": 0.0, "invalid_candidate": True,
                    "feedback": f"Candidate rejected, score 0: {error}. {GUIDE_FORMAT_NOTE}",
                } for t in batch]
            return EvaluationBatch(outputs=outputs, scores=scores, trajectories=trajs)

        comps = {**self.base_comps, **candidate}
        done = run_pipeline(self.ctx, batch, comps)
        outputs, scores, trajs = [], [], []
        for t in done:
            score, note = pipeline.score_txn(t)
            out = {
                "category": t.category, "category_confidence": t.category_confidence,
                "category_method": t.category_method, "candidates": list(t.candidates),
                "rule_id": t.rule_id, "gl_code": t.final_gl_code or t.gl_code,
                "select_method": t.select_method, "select_confidence": t.select_confidence,
            }
            outputs.append(out)
            scores.append(score)
            if capture_traces:
                trajs.append({
                    "inputs": _inputs(t), "outputs": out, "expected": self._expected(t),
                    "score": score, "invalid_candidate": False, "feedback": self._feedback(t, note),
                })
        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=trajs if capture_traces else None)

    @staticmethod
    def _expected(t: Txn) -> dict[str, str]:
        cat, gl = pipeline.labels(t)
        return {"category": cat, "gl_code": gl}

    def _feedback(self, t: Txn, note: str) -> str:
        if note == "correct":
            return "correct"
        exp_cat, exp_gl = pipeline.labels(t)
        parts = [note]
        if exp_cat and t.category.lower() != exp_cat.lower():
            conf = f"{t.category_confidence:.2f}" if t.category_confidence is not None else "n/a"
            parts.append(f"category confidence was {conf} (method: {t.category_method or 'n/a'})")
            parts.append("allowed categories: " + ", ".join(self.ctx.policy.categories))
        if exp_gl and exp_cat:
            legal = [r.rule_id for r in self.ctx.policy.eligible(exp_cat, t.department, t.amount)]
            parts.append(f"eligible rules for the expected category: {', '.join(legal) or 'none'}")
        return ". ".join(p.rstrip(".") for p in parts) + "."

    # --------------------------------------------------------- reflective dataset
    def make_reflective_dataset(self, candidate: dict[str, str], eval_batch, components_to_update: list[str]):
        trajs = eval_batch.trajectories or []
        rng = random.Random(self.seed)
        result: dict[str, list[dict[str, Any]]] = {}
        for comp in components_to_update:
            failures = [tr for tr in trajs if tr["score"] < 1.0]
            wins = [tr for tr in trajs if tr["score"] >= 1.0]
            if comp in ("category_guide", "categorize_instructions"):
                cat_fail = [tr for tr in failures if tr.get("invalid_candidate") or self._cat_failed(tr)]
                failures = cat_fail or failures
            elif comp == "select_instructions":
                sel_fail = [tr for tr in failures if not tr.get("invalid_candidate") and not self._cat_failed(tr)]
                failures = sel_fail or failures
            rng.shuffle(failures)
            rng.shuffle(wins)
            chosen = failures[:MAX_FAILURES_PER_COMPONENT] + wins[:MAX_SUCCESSES_PER_COMPONENT]
            records = []
            for tr in chosen:
                feedback = tr["feedback"]
                if comp == "category_guide" and not tr.get("invalid_candidate"):
                    feedback = f"{feedback} {GUIDE_FORMAT_NOTE}" if tr["score"] < 1.0 else feedback
                records.append({
                    "Inputs": tr["inputs"],
                    "Generated Outputs": tr["outputs"],
                    "Feedback": feedback,
                    "Expected": tr["expected"],
                })
            result[comp] = records
        return result

    @staticmethod
    def _cat_failed(tr: dict) -> bool:
        exp = tr["expected"]["category"]
        got = tr["outputs"].get("category", "")
        return bool(exp) and got.lower() != exp.lower()


# --------------------------------------------------------------------------- #
#  deterministic proposer for the mock backend
# --------------------------------------------------------------------------- #
_STOPWORDS = frozenset(
    "this that with from have been were your their there then than into over some more most other "
    "also just only like very much many here what when where which while about after before again "
    "for the and not inc llc corp ltd".split()
)
_WORD = re.compile(r"[a-z]+")


def _guide_lines(text: str) -> list[tuple[str, str, str]]:
    """(canonical-ish name, description, original line) per non-empty guide line."""
    out = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*• ").strip()
        if not line:
            continue
        name, _, desc = line.partition(":")
        out.append((name.strip(), desc.strip(), raw))
    return out


class MockProposer:
    """Offline stand-in for the reflection model. Deterministic.

    For each failed category record it appends up to ``per_record`` salient words from
    the transaction (note first, then cleaned vendor name) to the description
    of the expected category. Words already present anywhere in the guide are skipped,
    so a word cannot end up in two categories. The rebuilt guide must still parse; if
    it cannot, the component is returned unchanged.
    """

    def __init__(self, policy, per_record: int = 3) -> None:
        self.policy = policy
        self.per_record = per_record

    def __call__(self, candidate: dict[str, str], reflective_dataset, components_to_update, **kwargs) -> dict[str, str]:
        out: dict[str, str] = {}
        for comp in components_to_update:
            text = candidate.get(comp, "")
            if comp == "category_guide":
                text = self._improve_guide(text, reflective_dataset.get(comp, []))
            out[comp] = text
        return out

    def _salient(self, inputs: dict[str, Any]) -> list[str]:
        seen: list[str] = []
        # The note says what was bought and generalizes across merchants; the cleaned vendor
        # name is next. Raw statement text is a last resort (it carries cities and store codes).
        keys = ("note", "vendor") if inputs.get("vendor") else ("note", "description")
        for key in keys:
            for w in _WORD.findall(str(inputs.get(key, "")).lower()):
                if len(w) >= 4 and w not in _STOPWORDS and w not in seen:
                    seen.append(w)
        return seen

    def _improve_guide(self, guide: str, records) -> str:
        cats = self.policy.categories
        try:
            parse_category_guide(guide, cats)
        except ValueError:
            return guide
        lines = _guide_lines(guide)
        canon = {c.lower(): c for c in cats}
        present = {w for _, _, raw in lines for w in _WORD.findall(raw.lower())}
        additions: dict[str, list[str]] = {}
        for rec in records:
            exp = (rec.get("Expected") or {}).get("category", "")
            got = (rec.get("Generated Outputs") or {}).get("category", "")
            cat = canon.get(exp.lower())
            if not cat or got.lower() == exp.lower():
                continue
            added = 0
            for w in self._salient(rec.get("Inputs") or {}):
                if added >= self.per_record:
                    break
                if w in present:
                    continue
                present.add(w)
                additions.setdefault(cat, []).append(w)
                added += 1
        if not additions:
            return guide
        rebuilt = []
        for name, desc, raw in lines:
            cat = canon.get(name.lower())
            extra = additions.get(cat or "")
            if extra:
                joined = ", ".join(extra)
                desc = f"{desc}, {joined}" if desc else joined
                rebuilt.append(f"{name}: {desc}")
            else:
                rebuilt.append(raw.strip())
        new = "\n".join(rebuilt)
        try:
            parse_category_guide(new, cats)
        except ValueError:
            return guide
        return new
