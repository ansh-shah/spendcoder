"""Project configuration (``spendcoder.toml``).

Every path in the file is resolved relative to the directory that holds it, so a
project folder can be moved or committed as a unit.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAME = "spendcoder.toml"

CANONICAL_FIELDS = ("id", "date", "description", "note", "amount", "department")
REQUIRED_FIELDS = ("id", "description", "amount")


class ConfigError(ValueError):
    pass


@dataclass
class DatasetConfig:
    path: Path
    labeled_path: Path | None
    columns: dict[str, str]
    amount_sign: float = 1.0


@dataclass
class PolicyConfig:
    categories: Path
    rules: Path
    vendor_overrides: Path | None
    vendors: Path | None
    note_glossary: dict[str, str] = field(default_factory=dict)
    keyword_aliases: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class LLMConfig:
    backend: str = "openrouter"  # openrouter | agent-files | mock
    chat_model: str = "openai/gpt-5.6-terra"
    decision_model: str = "typesafe/jev-1.13"
    reflection_model: str = "openai/gpt-5.6-terra"
    reasoning_effort: str = "low"
    max_concurrency: int = 8
    timeout_sec: float = 120.0
    max_retries: int = 4
    normalize_batch_size: int = 40
    agent_work_dir: Path | None = None
    agent_timeout_sec: float = 7200.0


@dataclass
class SearchConfig:
    provider: str = "none"  # parallel | none
    mode: str = "fast"
    max_results: int = 3
    max_chars_per_result: int = 600
    only_unknown_merchants: bool = True


@dataclass
class GateConfig:
    category_min_confidence: float = 0.55
    select_min_confidence: float = 0.50
    max_error_rate: float = 0.05


@dataclass
class ReviewFlag:
    name: str
    question: str
    threshold: float = 0.5
    # "review": send the row to the reviewer. "hold": always unresolved (needs a person).
    action: str = "review"


@dataclass
class ReviewConfig:
    enabled: bool = True
    low_confidence: float = 0.5
    flags: list[ReviewFlag] = field(default_factory=list)


@dataclass
class TuneConfig:
    components: list[str] = field(
        default_factory=lambda: ["categorize_instructions", "category_guide", "select_instructions"]
    )
    max_metric_calls: int = 300
    minibatch_size: int = 6
    split: tuple[float, float, float] = (0.5, 0.25, 0.25)
    seed: int = 7


@dataclass
class Config:
    root: Path
    name: str
    dataset: DatasetConfig
    policy: PolicyConfig
    llm: LLMConfig
    search: SearchConfig
    gates: GateConfig
    review: ReviewConfig
    tune: TuneConfig
    prompts_dir: Path
    runs_dir: Path
    cache_dir: Path
    source_path: Path

    @property
    def components_path(self) -> Path:
        return self.prompts_dir / "components.json"


def _path(root: Path, value: Any, *, optional: bool = False) -> Path | None:
    if value in (None, ""):
        if optional:
            return None
        raise ConfigError("missing required path")
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (root / p)


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _typed(section: dict, key: str, default: Any, kind: type) -> Any:
    if key not in section:
        return default
    value = section[key]
    # bool is a subclass of int; never let `true` silently become 1.
    if kind in (int, float) and isinstance(value, bool):
        raise ConfigError(f"{key} must be a number, got a boolean")
    if kind is float and isinstance(value, int):
        return float(value)
    if not isinstance(value, kind):
        raise ConfigError(f"{key} must be {kind.__name__}, got {type(value).__name__}")
    return value


def find_config(start: Path) -> Path:
    start = start.resolve()
    if start.is_file():
        return start
    for d in (start, *start.parents):
        candidate = d / CONFIG_NAME
        if candidate.exists():
            return candidate
    raise ConfigError(f"no {CONFIG_NAME} found in {start} or its parents (run `spendcoder init`)")


def load_config(path: str | Path = ".") -> Config:
    cfg_path = find_config(Path(path))
    try:
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{cfg_path}: {e}") from e
    root = cfg_path.parent

    project = _section(raw, "project")
    ds = _section(raw, "dataset")
    columns = ds.get("columns", {})
    missing = [f for f in REQUIRED_FIELDS if f not in columns]
    if missing:
        raise ConfigError(f"[dataset.columns] must map: {', '.join(missing)}")
    unknown = sorted(set(columns) - set(CANONICAL_FIELDS))
    if unknown:
        raise ConfigError(f"[dataset.columns] unknown fields {unknown}; allowed {CANONICAL_FIELDS}")
    dataset = DatasetConfig(
        path=_path(root, ds.get("path")),
        labeled_path=_path(root, ds.get("labeled_path"), optional=True),
        columns={k: str(v) for k, v in columns.items()},
        amount_sign=_typed(ds, "amount_sign", 1.0, float),
    )

    pol = _section(raw, "policy")
    aliases = pol.get("keyword_aliases", {})
    policy = PolicyConfig(
        categories=_path(root, pol.get("categories")),
        rules=_path(root, pol.get("rules")),
        vendor_overrides=_path(root, pol.get("vendor_overrides"), optional=True),
        vendors=_path(root, pol.get("vendors"), optional=True),
        note_glossary={str(k): str(v) for k, v in pol.get("note_glossary", {}).items()},
        keyword_aliases={str(k).lower(): [str(a) for a in v] for k, v in aliases.items()},
    )

    l = _section(raw, "llm")
    llm = LLMConfig(
        backend=_typed(l, "backend", "openrouter", str),
        chat_model=_typed(l, "chat_model", LLMConfig.chat_model, str),
        decision_model=_typed(l, "decision_model", LLMConfig.decision_model, str),
        reflection_model=_typed(l, "reflection_model", LLMConfig.reflection_model, str),
        reasoning_effort=_typed(l, "reasoning_effort", "low", str),
        max_concurrency=_typed(l, "max_concurrency", 8, int),
        timeout_sec=_typed(l, "timeout_sec", 120.0, float),
        max_retries=_typed(l, "max_retries", 4, int),
        normalize_batch_size=_typed(l, "normalize_batch_size", 40, int),
        agent_work_dir=_path(root, l.get("agent_work_dir", "runs/agent_queue")),
        agent_timeout_sec=_typed(l, "agent_timeout_sec", 7200.0, float),
    )
    if llm.backend not in ("openrouter", "agent-files", "mock"):
        raise ConfigError(f"llm.backend must be openrouter | agent-files | mock, got {llm.backend!r}")

    s = _section(raw, "search")
    search = SearchConfig(
        provider=_typed(s, "provider", "none", str),
        mode=_typed(s, "mode", "fast", str),
        max_results=_typed(s, "max_results", 3, int),
        max_chars_per_result=_typed(s, "max_chars_per_result", 600, int),
        only_unknown_merchants=_typed(s, "only_unknown_merchants", True, bool),
    )
    if search.provider not in ("parallel", "none"):
        raise ConfigError(f"search.provider must be parallel | none, got {search.provider!r}")

    g = _section(raw, "gates")
    gates = GateConfig(
        category_min_confidence=_typed(g, "category_min_confidence", 0.55, float),
        select_min_confidence=_typed(g, "select_min_confidence", 0.50, float),
        max_error_rate=_typed(g, "max_error_rate", 0.05, float),
    )

    r = _section(raw, "review")
    flags = []
    for f in r.get("flags", []):
        if "name" not in f or "question" not in f:
            raise ConfigError("every [[review.flags]] needs name and question")
        action = str(f.get("action", "review"))
        if action not in ("review", "hold"):
            raise ConfigError(f"review flag {f['name']}: action must be review | hold")
        flags.append(ReviewFlag(str(f["name"]), str(f["question"]), float(f.get("threshold", 0.5)), action))
    review = ReviewConfig(
        enabled=_typed(r, "enabled", True, bool),
        low_confidence=_typed(r, "low_confidence", 0.5, float),
        flags=flags,
    )

    t = _section(raw, "tune")
    split = tuple(float(x) for x in t.get("split", (0.5, 0.25, 0.25)))
    if len(split) != 3 or abs(sum(split) - 1.0) > 1e-6:
        raise ConfigError("tune.split must be three fractions summing to 1")
    tune = TuneConfig(
        components=list(t.get("components", TuneConfig().components)),
        max_metric_calls=_typed(t, "max_metric_calls", 300, int),
        minibatch_size=_typed(t, "minibatch_size", 6, int),
        split=split,  # type: ignore[arg-type]
        seed=_typed(t, "seed", 7, int),
    )

    paths = _section(raw, "paths")
    return Config(
        root=root,
        name=str(project.get("name", root.name)),
        dataset=dataset,
        policy=policy,
        llm=llm,
        search=search,
        gates=gates,
        review=review,
        tune=tune,
        prompts_dir=_path(root, paths.get("prompts", "prompts")),
        runs_dir=_path(root, paths.get("runs", "runs")),
        cache_dir=_path(root, paths.get("cache", ".spendcoder_cache")),
        source_path=cfg_path,
    )
