"""Command-line interface: ``spendcoder <command>`` (also ``python -m spendcoder``).

The CLI only wires terminal arguments to ``pipeline``, ``tune`` and the agent queue.
Expected failures (bad config, policy, data, a stage gate, a backend, a file that
already exists) print one ``error:`` line to stderr and return 1, with no traceback.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

import spendcoder
from spendcoder import pipeline
from spendcoder.config import ConfigError, load_config
from spendcoder.data import DataError
from spendcoder.envfile import load_dotenv
from spendcoder.llm.agent_files import queue_answer, queue_list, queue_show
from spendcoder.llm.base import BackendError, InvalidAnswer, Ledger
from spendcoder.llm.openrouter import OpenRouterBackend
from spendcoder.pipeline import open_project
from spendcoder.policy import PolicyError
from spendcoder.stages import StageError

BACKENDS = ("openrouter", "agent-files", "mock")
SPLITS = ("train", "val", "test")
TEMPLATES = ("acme", "blank")

# Expected failures: reported as one line, exit 1. ValueError covers the stale-prompt
# and malformed-answer cases, and every project error class here subclasses it.
_EXPECTED = (ConfigError, PolicyError, DataError, StageError, BackendError, FileExistsError, ValueError)


def _say(msg: str = "") -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    cells = [header, *rows]
    widths = [max(len(str(r[i])) for r in cells) for i in range(len(header))]
    return ["  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]


def _templates_dir() -> Path:
    return Path(spendcoder.__file__).parent / "templates"


# --------------------------------------------------------------------------- #
#  commands
# --------------------------------------------------------------------------- #
def _cmd_init(args: argparse.Namespace) -> int:
    src = _templates_dir() / args.template
    dst = Path(args.dir)
    if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
        raise FileExistsError(f"{dst} already exists and is not an empty folder; choose a new one")
    shutil.copytree(src, dst, dirs_exist_ok=True)
    _say(f"created {dst} from the '{args.template}' template")
    _say("next steps:")
    _say(f"  cd {dst}")
    _say("  spendcoder doctor")
    _say("  spendcoder onboard --backend mock --apply")
    _say("  spendcoder run --backend mock")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.project)
    if args.backend:
        cfg.llm.backend = args.backend
    problems, notes = pipeline.doctor(cfg)
    for n in notes:
        _say(f"  - {n}")
    if problems:
        for p in problems:
            _say(f"  x {p}")
        return 1
    _say("ready")
    return 0


def _cmd_onboard(args: argparse.Namespace) -> int:
    from spendcoder.tune import onboard

    comps = [c.strip() for c in args.components.split(",") if c.strip()] if args.components else None
    ctx = open_project(args.project, args.backend)
    try:
        res = onboard(ctx, apply=args.apply, budget=args.budget, components=comps, log=_say)
    except _EXPECTED:
        raise
    except RuntimeError as e:  # e.g. GEPA not installed (the 'tune' extra)
        _err(f"error: {e}")
        return 2

    header = ["split", "version", "rows", "score", "category", "GL"]
    rows = []
    for split in SPLITS:
        for label, scores in (("baseline", res["baseline"]), ("tuned", res["tuned"])):
            m = scores[split]
            rows.append([split, label, str(m["rows"]), f"{m['score']:.3f}",
                         _pct(m["category_accuracy"]), _pct(m["gl_accuracy"])])
    _say("")
    for line in _table(header, rows):  # promotion reason and report path were already logged by onboard()
        _say(line)
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from spendcoder.tune import evaluate_components, load_labeled

    ctx = open_project(args.project, args.backend)
    try:
        txns = load_labeled(ctx)
        m = evaluate_components(ctx, txns, ctx.comps)
    except _EXPECTED:
        raise
    except RuntimeError as e:  # e.g. no labeled set configured
        _err(f"error: {e}")
        return 1
    _say(f"labeled rows: {m['rows']}")
    _say(f"score: {m['score']:.3f}")
    _say(f"category accuracy: {_pct(m['category_accuracy'])}")
    _say(f"GL accuracy: {_pct(m['gl_accuracy'])}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    ctx = open_project(args.project, args.backend)
    out = pipeline.run(ctx, Path(args.input) if args.input else None, args.run_id, args.limit, log=_say)
    _say(str(out / "summary.md"))
    return 0


def _work_dir(args: argparse.Namespace) -> Path:
    return load_config(args.project).llm.agent_work_dir


def _cmd_queue_list(args: argparse.Namespace) -> int:
    rows = queue_list(_work_dir(args))
    if not rows:
        _say("queue empty")
        return 0
    for line in _table(["id", "kind", "purpose", "created_at"],
                       [[r["id"], r["kind"], r["purpose"], r["created_at"]] for r in rows]):
        _say(line)
    return 0


def _cmd_queue_show(args: argparse.Namespace) -> int:
    req = queue_show(_work_dir(args), args.id)
    _say(json.dumps(req, indent=2, ensure_ascii=False))
    return 0


def _cmd_queue_answer(args: argparse.Namespace) -> int:
    path = Path(args.file)
    try:
        output: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _err(f"error: cannot read answer file {path}: {e}")
        return 1
    try:
        queue_answer(_work_dir(args), args.id, output)
    except InvalidAnswer as e:
        _err(f"error: {e}")
        return 1
    _say("accepted")
    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    cfg = load_config(args.project)
    load_dotenv(cfg.root)
    models = OpenRouterBackend(cfg.llm, Ledger()).list_decision_models()
    if not models:
        _say("no decision models returned")
        return 0
    rows = []
    for m in models:
        price = (m.get("pricing") or {}).get("prompt")
        rows.append([str(m.get("id", "")), str(m.get("context_length") or "-"), str(price or "-")])
    for line in _table(["id", "context_length", "prompt price"], rows):
        _say(line)
    return 0


# --------------------------------------------------------------------------- #
#  parser
# --------------------------------------------------------------------------- #
def _add_backend(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", choices=BACKENDS, default=None,
                   help="LLM backend for this run (default: [llm] backend in spendcoder.toml)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spendcoder", description="Agentic GL coding for corporate card spend.")
    p.add_argument("--project", default=".", metavar="PATH",
                   help="project folder or spendcoder.toml (default: current folder, searching parents)")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    s = sub.add_parser("init", help="create a project from a template")
    s.add_argument("dir", metavar="DIR")
    s.add_argument("--template", choices=TEMPLATES, default="acme")
    s.set_defaults(func=_cmd_init)

    s = sub.add_parser("doctor", help="validate config, policy and data")
    _add_backend(s)
    s.set_defaults(func=_cmd_doctor)

    s = sub.add_parser("onboard", help="tune prompt wording with GEPA against data/labeled.csv")
    s.add_argument("--apply", action="store_true", help="write tuned prompts if the held-out test score holds up")
    s.add_argument("--budget", type=int, default=None, metavar="N", help="GEPA metric-call budget")
    s.add_argument("--components", default=None, metavar="a,b", help="comma-separated components to tune")
    _add_backend(s)
    s.set_defaults(func=_cmd_onboard)

    s = sub.add_parser("eval", help="score current prompts on the labeled set")
    _add_backend(s)
    s.set_defaults(func=_cmd_eval)

    s = sub.add_parser("run", help="code a card export and write a run directory")
    s.add_argument("--input", default=None, metavar="PATH", help="card export (default: [dataset] path)")
    s.add_argument("--run-id", default=None, metavar="ID", help="run folder name (default: timestamp)")
    _add_backend(s)
    s.add_argument("--limit", type=int, default=None, metavar="N", help="code only the first N rows")
    s.set_defaults(func=_cmd_run)

    q = sub.add_parser("queue", help="agent-files request queue")
    qs = q.add_subparsers(dest="queue_command", required=True, metavar="action")
    s = qs.add_parser("list", help="open requests")
    s.set_defaults(func=_cmd_queue_list)
    s = qs.add_parser("show", help="print one request")
    s.add_argument("id")
    s.set_defaults(func=_cmd_queue_show)
    s = qs.add_parser("answer", help="validate and submit an answer file")
    s.add_argument("id")
    s.add_argument("file", metavar="FILE")
    s.set_defaults(func=_cmd_queue_answer)

    s = sub.add_parser("models", help="list OpenRouter decision models")
    s.set_defaults(func=_cmd_models)
    return p


def _configure_stdio() -> None:
    """Never crash on a character the Windows console cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except _EXPECTED as e:
        _err(f"error: {e}")
        return 1
