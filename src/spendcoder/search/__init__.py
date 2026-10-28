"""Web evidence for unknown merchants."""
from __future__ import annotations

from .parallel import NullSearch, ParallelSearch, SearchClient, make_search

__all__ = ["NullSearch", "ParallelSearch", "SearchClient", "make_search"]
