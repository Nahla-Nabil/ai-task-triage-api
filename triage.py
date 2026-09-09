"""POST /tasks/triage's business logic. Same shape as auth.py, db.py and
cache.py: main.py's route only ever calls triage.run_triage() — nothing
outside this file knows how a triage judgement actually gets made.

Stage 3: the model's answer is now treated as untrusted input, exactly
like Week 6's external data — parsed, validated against the schema, given
one repair attempt if it fails, and quarantined (never crashed, never
handed to the caller as-is) if it fails twice. Stage 4 adds a real timeout,
a retry policy, cost logging and a kill switch around the call itself.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pydantic

from llm import client as llm_client
from llm.schema import TriageResult

PROMPT_VERSION = "triage-v1"
PROMPT_PATH = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"
QUARANTINE_PATH = Path(__file__).parent / "logs" / "quarantine.jsonl"


class TriageError(Exception):
    """Carries a status code and message through to main.py's exception
    handler, same pattern as auth.AuthError."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


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


def parse_json_object(raw: str) -> dict:
    """Models like to wrap JSON in a ```json fence or add a sentence in
    front of it. Strip that noise, then hand json.loads only the object
    itself. Raises ValueError (not json.JSONDecodeError directly) so
    callers have one exception type to catch regardless of what went wrong."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first_line, rest = text.split("\n", 1)
            if first_line.strip().lower() in ("json", ""):
                text = rest
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("model output did not contain a JSON object")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"model output was not valid JSON: {exc}") from exc


def _parse_and_validate(raw_text: str) -> dict:
    """Raises ValueError or pydantic.ValidationError on anything short of a
    schema-valid object — the one place run_triage() decides "did this
    attempt succeed"."""
    return TriageResult.model_validate(parse_json_object(raw_text)).model_dump()


def _stub_result(text: str) -> TriageResult:
    """LLM_STUB=1 — a fixed, schema-valid object, no model call. Used for
    every restart-the-server iteration; see README for why this exists."""
    return TriageResult(
        category="other",
        priority="normal",
        clean_title=text.strip()[:80] or "Untitled task",
        confidence=0.42,
    )


def _quarantine(text: str, raw_output: str, error: str) -> None:
    """Both attempts failed. Set the bad answer aside instead of crashing
    or reaching a database with it — same "quarantine untrusted data"
    instinct as Week 6, applied to a model instead of a scraper."""
    QUARANTINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with QUARANTINE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prompt_version": PROMPT_VERSION,
            "input_text": text,
            "raw_output": raw_output,
            "error": error,
        }) + "\n")


def run_triage(text: str) -> dict:
    """The pipeline for one request. Input validation already happened in
    the route (main.py) before this function is ever called."""
    if os.environ.get("LLM_STUB") == "1":
        return _stub_result(text).model_dump()

    system_prompt = load_prompt()
    messages = build_messages(text, system_prompt)

    raw_text = llm_client.complete(messages)
    try:
        return _parse_and_validate(raw_text)
    except (ValueError, pydantic.ValidationError) as first_error:
        # Repair retry: send the model its own broken answer plus the exact
        # error, and ask once — and only once — for a corrected object.
        repair_messages = messages + [
            {"role": "assistant", "content": raw_text},
            {"role": "user", "content": (
                "Your previous answer was rejected for this reason: "
                f"{first_error}. Return only corrected JSON matching the schema."
            )},
        ]
        repaired_text = llm_client.complete(repair_messages)
        try:
            return _parse_and_validate(repaired_text)
        except (ValueError, pydantic.ValidationError) as second_error:
            _quarantine(text, repaired_text, str(second_error))
            raise TriageError(422, "The model could not produce a valid triage result") from second_error
