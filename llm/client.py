"""The one module that talks to the model provider. Same shape as auth.py,
db.py and cache.py: every line that knows about the OpenAI-compatible HTTP
API lives here, so triage.py (and any future AI job) never imports
`openai` directly.

Three environment variables are the entire difference between calling a
model on this machine and calling one in a datacentre:

    LLM_BASE_URL   e.g. https://openrouter.ai/api/v1
    LLM_API_KEY    your OpenRouter key (or the literal string "ollama"
                   for a local Ollama server)
    LLM_MODEL      e.g. openrouter/free, or gemma3:1b for Ollama

Nothing below this module knows or cares which provider those three values
point at.
"""

import os
import random
import time

import openai
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openrouter/free")

# The official SDK defaults to a 10-minute timeout and 2 automatic retries.
# Both are wrong for something sitting behind an HTTP endpoint a caller is
# waiting on — so both are turned off here (timeout set explicitly below,
# max_retries=0) and replaced with the explicit policy in complete_with_retry().
LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "2"))


class LLMCallResult:
    """What complete_with_retry() hands back: the raw text plus everything
    Stage 4's cost log needs, and nothing a caller has to reach into the
    OpenAI SDK's response object to get."""

    def __init__(self, text: str, input_tokens: int, output_tokens: int, attempts: int):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.attempts = attempts


def get_client() -> OpenAI:
    """A fresh client per call — same reasoning as auth.get_client(): cheap
    to construct, and it keeps this module free of shared mutable state."""
    return OpenAI(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=0,  # we retry ourselves, deliberately, see below
    )


def _retryable(exc: Exception) -> tuple[bool, float | None]:
    """Decides whether `exc` deserves another attempt, and how long to wait
    if the server told us. Yes on timeouts, 429, and 5xx. Never on 400, 401
    or 403 — a bad key or a bad request will still be bad in four seconds,
    and on a metered free tier every pointless retry burns real quota."""
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return True, None
    if isinstance(exc, openai.APIStatusError):
        retry_after = None
        header = exc.response.headers.get("retry-after") if exc.response is not None else None
        if header is not None:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None  # Retry-After can also be an HTTP date; not handled here
        if exc.status_code == 429 or exc.status_code >= 500:
            return True, retry_after
        return False, None  # 400 / 401 / 403 / other 4xx — never retried
    return False, None


def complete_with_retry(messages: list[dict], temperature: float = 0.2) -> LLMCallResult:
    """Calls the model with an explicit timeout and a bounded retry policy:
    exponential backoff (1s, 2s, 4s, ...) plus jitter, capped at
    LLM_MAX_RETRIES extra attempts, honouring a Retry-After header when the
    provider sends one instead of guessing. Raises the last exception if
    every attempt is exhausted."""
    client = get_client()
    attempt = 0
    while True:
        attempt += 1
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                temperature=temperature,
            )
            usage = response.usage
            return LLMCallResult(
                text=response.choices[0].message.content or "",
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                attempts=attempt,
            )
        except Exception as exc:
            should_retry, retry_after = _retryable(exc)
            if not should_retry or attempt > LLM_MAX_RETRIES:
                raise
            wait = retry_after if retry_after is not None else (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            time.sleep(wait)
