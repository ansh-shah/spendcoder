"""Your accounting policy: categories, the rule matrix, and vendor references.

Rules are data, not prompts. A rule says: transactions in *category*, from
*department* (blank = any) and not from *excluded departments*, within an
*amount range* (blank = any), post to *GL code / GL name*. Optional *keywords*
let the selector break ties literally when several rules match.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from spendcoder.config import PolicyConfig

RULE_COLUMNS = ("rule_id", "category", "department", "excluded_departments",
                "amount_range", "keywords", "gl_code", "gl_name")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    gl_code: str
    gl_name: str
    department: str = ""
    excluded_departments: tuple[str, ...] = ()
    amount_min: float | None = None
    amount_max: float | None = None
    keywords: tuple[str, ...] = ()

    def amount_ok(self, amount: float) -> bool:
        if self.amount_min is None:
            return True
        if amount != amount:  # NaN never satisfies a ranged rule
            return False
        return self.amount_min <= abs(amount) <= self.amount_max  # refunds match like charges

    def describe(self) -> str:
        kw = ", ".join(self.keywords) if self.keywords else "(none: default for its scope)"
        return f"{self.gl_name} ({self.gl_code}); keywords: {kw}"


@dataclass
class Policy:
    categories: dict[str, str]                       # name -> description
    rules: list[Rule]
    vendor_overrides: dict[str, str] = field(default_factory=dict)  # vendor -> category
    vendors: list[str] = field(default_factory=list)
    note_glossary: dict[str, str] = field(default_factory=dict)
    keyword_aliases: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_id = {r.rule_id: r for r in self.rules}
        self.gl_pairs = {(r.gl_code, r.gl_name) for r in self.rules}

    def eligible(self, category: str, department: str, amount: float) -> list[Rule]:
        """Deterministic rule match. This is the only place eligibility is decided."""
        cat = category.strip().lower()
        dept = department.strip().lower()
        out = []
        for r in self.rules:
            if r.category.lower() != cat:
                continue
            if r.department and r.department.lower() != dept:
                continue
            if dept and dept in (d.lower() for d in r.excluded_departments):
                continue
            if not r.amount_ok(amount):
                continue
            out.append(r)
        return out


_RANGE = re.compile(r"^\$?(\d+(?:\.\d+)?)\s*(?:-\s*\$?(\d+(?:\.\d+)?))?$")


def parse_amount_range(text: str, rule_id: str) -> tuple[float | None, float | None]:
    text = text.replace(",", "").strip()
    if not text:
        return None, None
    m = _RANGE.match(text)
    if not m:
        # Fail closed: an unreadable range would otherwise match every amount.
        raise PolicyError(f"rule {rule_id}: unreadable amount_range {text!r} (use '100' or '0-99.99')")
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    if lo > hi:
        raise PolicyError(f"rule {rule_id}: amount_range min > max")
    return lo, hi


def _split(text: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in text.split(",") if p.strip())


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise PolicyError(f"file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]


def load_policy(cfg: PolicyConfig) -> Policy:
    cat_rows = _read_csv(cfg.categories)
    if not cat_rows or "category" not in cat_rows[0]:
        raise PolicyError(f"{cfg.categories.name}: needs columns category, description")
    categories: dict[str, str] = {}
    for r in cat_rows:
        name = r["category"]
        if not name:
            continue
        if name.lower() in (c.lower() for c in categories):
            raise PolicyError(f"duplicate category {name!r}")
        categories[name] = r.get("description", "")

    rules: list[Rule] = []
    rule_rows = _read_csv(cfg.rules)
    if rule_rows:
        missing = [c for c in RULE_COLUMNS if c not in rule_rows[0]]
        if missing:
            raise PolicyError(f"{cfg.rules.name}: missing columns {missing}")
    seen = set()
    lower_cats = {c.lower(): c for c in categories}
    for r in rule_rows:
        rid = r["rule_id"]
        if not rid:
            if any(r.values()):
                raise PolicyError(f"{cfg.rules.name}: a rule row has no rule_id ({r.get('gl_code') or r})")
            continue
        if rid in seen:
            raise PolicyError(f"duplicate rule_id {rid!r}")
        seen.add(rid)
        if r["category"].lower() not in lower_cats:
            raise PolicyError(f"rule {rid}: category {r['category']!r} is not in {cfg.categories.name}")
        if not r["gl_code"] or not r["gl_name"]:
            raise PolicyError(f"rule {rid}: gl_code and gl_name are required")
        lo, hi = parse_amount_range(r["amount_range"], rid)
        rules.append(Rule(
            rule_id=rid,
            category=lower_cats[r["category"].lower()],
            gl_code=r["gl_code"],
            gl_name=r["gl_name"],
            department=r["department"],
            excluded_departments=_split(r["excluded_departments"]),
            amount_min=lo,
            amount_max=hi,
            keywords=_split(r["keywords"]),
        ))

    overrides: dict[str, str] = {}
    if cfg.vendor_overrides and cfg.vendor_overrides.exists():
        for r in _read_csv(cfg.vendor_overrides):
            vendor, cat = r.get("vendor", ""), r.get("category", "")
            if not vendor:
                continue
            if cat.lower() not in lower_cats:
                raise PolicyError(f"vendor override {vendor!r}: unknown category {cat!r}")
            overrides[vendor] = lower_cats[cat.lower()]

    vendors: list[str] = []
    if cfg.vendors and cfg.vendors.exists():
        vendors = [ln.strip() for ln in cfg.vendors.read_text(encoding="utf-8").splitlines() if ln.strip()]

    return Policy(categories, rules, overrides, vendors, dict(cfg.note_glossary), dict(cfg.keyword_aliases))


_PROCESSOR_PREFIX = re.compile(r"^(SQ ?\*|SP ?\*|TST ?\*|POS ?\*|PP ?\*|PAYPAL ?\*)", re.I)


def descriptor_key(description: str) -> str:
    """Collapse store numbers, processor prefixes and punctuation so lookalike
    descriptors ("SHELL #4567", "SHELL 0021") share one key."""
    s = _PROCESSOR_PREFIX.sub("", description.upper())
    s = re.sub(r"[#*]\s*\d+|\b\d{3,}\b", " ", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return " ".join(s.split()[:3])


def match_reference_vendor(description: str, vendors: list[str]) -> str | None:
    """Token-overlap match against the reference vendor list."""
    cleaned = re.sub(r"[^\w\s]", " ", _PROCESSOR_PREFIX.sub("", description.upper()))
    desc = {w for w in cleaned.split() if len(w) > 2}
    best, best_score = None, 0.0
    for vendor in vendors:
        tokens = [w for w in re.sub(r"[^\w\s]", " ", vendor.upper()).split() if len(w) > 2]
        if not tokens:
            continue
        score = sum(w in desc for w in tokens) / len(tokens)
        need = 1.0 if len(tokens) == 1 else 0.6
        if score >= need and score > best_score:
            best, best_score = vendor, score
    return best
