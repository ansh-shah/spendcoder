"""Load a card export into canonical transactions, and write results back out.

The input file is never modified. Original columns are carried through to the
export in their original order; spendcoder's columns are appended after them.
"""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spendcoder.config import DatasetConfig


class DataError(ValueError):
    pass


@dataclass
class Txn:
    id: str
    description: str
    amount: float
    note: str = ""
    department: str = ""
    date: str = ""
    raw: dict[str, str] = field(default_factory=dict)

    # filled by the stages
    vendor: str = ""
    vendor_method: str = ""          # model | override | fallback
    web_evidence: list[dict[str, str]] = field(default_factory=list)
    category: str = ""               # "" = unknown
    category_confidence: float | None = None
    category_method: str = ""        # decision | override | gated | error
    candidates: list[str] = field(default_factory=list)
    rule_id: str = ""
    gl_code: str = ""
    gl_name: str = ""
    select_method: str = ""          # unique | keyword | default | decision | gated | none
    select_confidence: float | None = None
    select_reason: str = ""
    flags: list[str] = field(default_factory=list)
    review_status: str = ""          # "" | kept | changed | unresolved
    review_reason: str = ""
    final_rule_id: str = ""
    final_gl_code: str = ""
    final_gl_name: str = ""

    def evidence_view(self) -> dict[str, Any]:
        """The only fields a model is shown. Never includes the issuer's own coding."""
        view: dict[str, Any] = {"description": self.description, "note": self.note}
        if self.department:
            view["department"] = self.department
        view["amount"] = round(self.amount, 2)
        if self.vendor:
            view["vendor"] = self.vendor
        if self.category:
            view["category"] = self.category
        if self.web_evidence:
            view["web_evidence"] = self.web_evidence
        return view


OUTPUT_COLUMNS = [
    "sc_vendor", "sc_vendor_method", "sc_category", "sc_category_confidence", "sc_category_method",
    "sc_candidate_rules", "sc_rule_id", "sc_gl_code", "sc_gl_name", "sc_select_method",
    "sc_select_confidence", "sc_select_reason", "sc_flags", "sc_review_status", "sc_review_reason",
    "sc_final_rule_id", "sc_final_gl_code", "sc_final_gl_name",
]


def sniff_delimiter(path: Path) -> str:
    sample = path.read_text(encoding="utf-8-sig", errors="replace")[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        raise DataError(f"file not found: {path}")
    delim = sniff_delimiter(path)
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=delim)
            rows = [{k: (v or "") for k, v in r.items() if k is not None} for r in reader]
            header = list(reader.fieldnames or [])
    except UnicodeDecodeError as e:
        raise DataError(f"{path} is not UTF-8 ({e}); re-export it as UTF-8 CSV") from e
    return header, rows


_AMOUNT_JUNK = re.compile(r"[,$\s]")


def parse_amount(value: str) -> float:
    s = _AMOUNT_JUNK.sub("", str(value))
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if not s:
        raise ValueError("empty amount")
    v = float(s)
    if not math.isfinite(v):
        raise ValueError(f"non-finite amount {value!r}")
    return -v if neg else v


def load_transactions(ds: DatasetConfig, path: Path | None = None) -> tuple[list[str], list[Txn]]:
    path = path or ds.path
    header, rows = read_rows(path)
    cols = ds.columns
    absent = [f"{k} -> {v!r}" for k, v in cols.items() if v not in header]
    if absent:
        raise DataError(f"{path.name}: mapped columns not in header: {', '.join(absent)}")

    txns: list[Txn] = []
    seen: set[str] = set()
    bad_amounts = []
    for n, row in enumerate(rows, start=2):
        tid = row[cols["id"]].strip()
        if not tid:
            raise DataError(f"{path.name} line {n}: empty transaction id")
        if tid in seen:
            raise DataError(f"{path.name} line {n}: duplicate transaction id {tid!r}")
        seen.add(tid)
        try:
            amount = parse_amount(row[cols["amount"]]) * ds.amount_sign
        except ValueError:
            bad_amounts.append(n)
            amount = float("nan")
        txns.append(Txn(
            id=tid,
            description=row[cols["description"]].strip(),
            amount=amount,
            note=row.get(cols.get("note", ""), "").strip() if "note" in cols else "",
            department=row.get(cols.get("department", ""), "").strip() if "department" in cols else "",
            date=row.get(cols.get("date", ""), "").strip() if "date" in cols else "",
            raw=row,
        ))
    if bad_amounts:
        lines = ", ".join(map(str, bad_amounts[:10]))
        raise DataError(f"{path.name}: unparseable amounts on line(s) {lines}")
    return header, txns


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3f}"
    if isinstance(v, list):
        return "|".join(str(x) for x in v)
    return str(v)


def txn_output_fields(t: Txn) -> dict[str, str]:
    return {
        "sc_vendor": t.vendor,
        "sc_vendor_method": t.vendor_method,
        "sc_category": t.category,
        "sc_category_confidence": _fmt(t.category_confidence),
        "sc_category_method": t.category_method,
        "sc_candidate_rules": _fmt(t.candidates),
        "sc_rule_id": t.rule_id,
        "sc_gl_code": t.gl_code,
        "sc_gl_name": t.gl_name,
        "sc_select_method": t.select_method,
        "sc_select_confidence": _fmt(t.select_confidence),
        "sc_select_reason": t.select_reason,
        "sc_flags": _fmt(t.flags),
        "sc_review_status": t.review_status,
        "sc_review_reason": t.review_reason,
        "sc_final_rule_id": t.final_rule_id,
        "sc_final_gl_code": t.final_gl_code,
        "sc_final_gl_name": t.final_gl_name,
    }


def write_output(path: Path, header: list[str], txns: list[Txn]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clash = [c for c in OUTPUT_COLUMNS if c in header]
    if clash:
        raise DataError(f"input already has spendcoder columns {clash}; pass the original export")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header + OUTPUT_COLUMNS)
        w.writeheader()
        for t in txns:
            w.writerow({**{h: t.raw.get(h, "") for h in header}, **txn_output_fields(t)})
    tmp.replace(path)
