"""Backward-compatible entry point.

The real report surface lives in :mod:`fasteditbench.report_app` as of
M4. This module re-exports its ``app`` so existing callers (e.g.
``uvicorn fasteditbench.main:app``) keep working.
"""

from fasteditbench.report_app import app

__all__ = ["app"]
