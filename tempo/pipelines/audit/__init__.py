"""Audit pipeline — privacy gate using contextual integrity + BM25 search."""

from .pipeline import AuditPipeline
from .bm25 import setup_fts, bm25_search

__all__ = ["AuditPipeline", "setup_fts", "bm25_search"]
