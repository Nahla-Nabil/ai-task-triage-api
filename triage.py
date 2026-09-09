"""POST /tasks/triage's business logic: build the prompt, call the model,
parse + validate + repair its answer, and log what it cost. Same shape as
auth.py, db.py and cache.py — main.py's route only ever calls
triage.run_triage(); nothing outside this file knows how a triage judgement
actually gets made.

The six-line version of what happens below:

    validate the input       -> done by the route, before this module runs
    build the prompt         -> load_prompt() + build_messages()
    call the model            -> llm.client.complete_with_retry()
    parse + validate output  -> parse_json_object() + TriageResult
    repair once if it failed -> _attempt() called a second time, with the error
    return clean JSON        -> TriageResult.model_dump(), or a TriageError
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import openai
import pydantic

from llm import client as llm_client
from llm.schema import TriageResult

PROMPT_VERSION = "triage-v1"
PROMPT_PATH = Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md"
QUARANTINE_PATH = Path(__file__).parent / "logs" / "quarantine.jsonl"


class TriageError(Exception):
    """Carries a status code and message through to main.py's exception
    handler, same pattern as auth.AuthError. Used for both "the model
    could not produce a valid answer" (422) and "the provider itself
    failed" (504/503)."""

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


def _stub_result(text: str) -> TriageResult:
    """LLM_STUB=1 — a fixed, schema-valid object, no model call. Used for
    every restart-the-server iteration; see README for why this exists."""
    return TriageResult(
        category="other",
        priority="normal",
        clean_title=text.strip()[:80] or "Untitled task",
        confidence=0.42,
    )


def _fallback_result(text: str) -> TriageResult:
    """LLM_ENABLED=false — the kill switch. Deterministic, no model call,
    schema-valid: a caller downstream still gets a usable object instead of
    a broken feature during a provider outage or a bill spike."""
    return TriageResult(
        category="other",
        priority="normal",
        clean_title=text.strip()[:80] or "Untitled task",
        confidence=0.0,
    )


def _log_cost(model: str, input_tokens: int, output_tokens: int, duration_ms: float, repaired: bool, stubbed: bool) -> None:
    """One structured JSON line per call, to stdout — Twelve-Factor style,
    no invented log file. Stage 4's "how much will this cost at 10k/day"
    question is answered by multiplying this line's tokens by the price
    calculator, not by guessing."""
    print(json.dumps({
        "event": "llm_call",
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": round(duration_ms, 1),
        "repaired": repaired,
        "stubbed": stubbed,
    }))


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


def _call_model(messages: list[dict], repaired: bool) -> str:
    """Wraps llm.client.complete_with_retry(): turns provider failures that
    survive the retry policy into the status codes the README promises
    (504 for "too slow", 503 for "the provider itself is broken"), and logs
    what the call cost either way. Used for both the first attempt and the
    one repair attempt — `repaired` just says which, for the cost log."""
    start = time.monotonic()
    try:
        result = llm_client.complete_with_retry(messages)
    except (openai.APITimeoutError, openai.APIConnectionError) as exc:
        raise TriageError(504, "The model took too long to respond") from exc
    except openai.AuthenticationError as exc:
        raise TriageError(503, "The model provider rejected our credentials") from exc
    except openai.APIStatusError as exc:
        raise TriageError(503, f"The model provider returned an error ({exc.status_code})") from exc
    duration_ms = (time.monotonic() - start) * 1000
    _log_cost(llm_client.LLM_MODEL, result.input_tokens, result.output_tokens, duration_ms, repaired, stubbed=False)
    return result.text


def _parse_and_validate(raw_text: str) -> dict:
    """Raises ValueError or pydantic.ValidationError on anything short of a
    schema-valid object — the one place run_triage() decides "did this
    attempt succeed"."""
    return TriageResult.model_validate(parse_json_object(raw_text)).model_dump()


def run_triage(text: str) -> dict:
    """The whole pipeline for one request. Input validation already
    happened in the route (main.py) before this function is ever called —
    every call in here is one that has already earned its cost."""
    if os.environ.get("LLM_STUB") == "1":
        return _stub_result(text).model_dump()

    if os.environ.get("LLM_ENABLED", "true").lower() == "false":
        return _fallback_result(text).model_dump()

    system_prompt = load_prompt()
    messages = build_messages(text, system_prompt)

    raw_text = _call_model(messages, repaired=False)
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
        repaired_text = _call_model(repair_messages, repaired=True)
        try:
            return _parse_and_validate(repaired_text)
        except (ValueError, pydantic.ValidationError) as second_error:
            _quarantine(text, repaired_text, str(second_error))
            raise TriageError(422, "The model could not produce a valid triage result") from second_error
