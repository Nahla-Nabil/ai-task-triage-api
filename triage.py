"""POST /tasks/triage's business logic. Same shape as auth.py, db.py and
cache.py: main.py's route only ever calls triage.run_triage() — nothing
outside this file knows how a triage judgement actually gets made.

Stage 1: only the input/output contract and stub mode are wired up here.
No model call yet — that arrives in Stage 2, with parsing, validation,
repair and quarantine following in Stage 3.
"""

import os

from llm.schema import TriageResult


def _stub_result(text: str) -> TriageResult:
    """LLM_STUB=1 — a fixed, schema-valid object, no model call. Lets the
    endpoint (and every route that will eventually call it) be built and
    tested without spending a single request against a real provider."""
    return TriageResult(
        category="other",
        priority="normal",
        clean_title=text.strip()[:80] or "Untitled task",
        confidence=0.42,
    )


def run_triage(text: str) -> dict:
    """The pipeline for one request. Input validation already happened in
    the route (main.py) before this function is ever called."""
    if os.environ.get("LLM_STUB") == "1":
        return _stub_result(text).model_dump()
    raise NotImplementedError("real model calls are wired up in Stage 2")
