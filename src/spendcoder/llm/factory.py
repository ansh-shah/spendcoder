"""Backend selection."""
from __future__ import annotations

from ..config import Config
from ..envfile import load_dotenv
from .agent_files import AgentFilesBackend
from .base import Backend, BackendError, Ledger
from .cache import CachedBackend
from .mock import MockBackend
from .openrouter import OpenRouterBackend


def make_backend(cfg: Config, ledger: Ledger, backend: str | None = None) -> Backend:
    load_dotenv(cfg.root)
    name = backend or cfg.llm.backend
    if name == "openrouter":
        return CachedBackend(OpenRouterBackend(cfg.llm, ledger), cfg.cache_dir / "llm", ledger)  # type: ignore[return-value]
    if name == "agent-files":
        work_dir = cfg.llm.agent_work_dir or (cfg.runs_dir / "agent_queue")
        return AgentFilesBackend(work_dir, cfg.llm.agent_timeout_sec, ledger)
    if name == "mock":
        return MockBackend(ledger)
    raise BackendError(f"unknown backend {name!r}; expected openrouter | agent-files | mock")
