"""POST /tasks/triage's business logic. Same shape as auth.py, db.py and
cache.py: main.py's route only ever calls triage.run_triage() — nothing
outside this file knows how a triage judgement actually gets made.

Stage 2: the prompt is loaded from its own versioned file and the model is
called for real — but per the assignment's own instruction, this stage
"just returns whatever text comes back". Stage 3 adds parsing, schema
validation, the repair retry, and quarantine; until then a real call's
answer is not yet safe to trust as-is.
"""

import json
import os
from pathlib import Path

from llm import client as llm_client
from llm.schema import TriageResult

PROMPT_VERSION = "triage-v1"
PROMPT_PATH = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"


def load_prompt() -> str:
    """The prompt lives in a file, not a string in this module — see
    prompts/triage-v1.md's own header comment for why that matters."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_messages(text: str, system_prompt: str) -> list[dict]:
    """The task text always travels as its own user message, JSON-encoded,
    never concatenated into the system prompt — the cheap defence against
    prompt injection described in JOB-CARD.md and prompts/triage-v1.md."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps({"text": text})},
    ]


def _stub_result(text: str) -> TriageResult:
    """LLM_STUB=1 — a fixed, schema-valid object, no model call. Used for
    every restart-the-server iteration; see README for why this exists."""
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

    system_prompt = load_prompt()
    messages = build_messages(text, system_prompt)
    raw_text = llm_client.complete(messages)
    return {"raw_output": raw_text}
