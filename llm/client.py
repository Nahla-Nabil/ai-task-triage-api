"""The one module that talks to the model provider. Same shape as auth.py,
db.py and cache.py: every line that knows about the OpenAI-compatible HTTP
API lives here, so triage.py never imports `openai` directly.

Three environment variables are the entire difference between calling a
model on this machine and calling one in a datacentre:

    LLM_BASE_URL   e.g. https://openrouter.ai/api/v1
    LLM_API_KEY    your OpenRouter key (or the literal string "ollama"
                   for a local Ollama server)
    LLM_MODEL      e.g. openrouter/free, or gemma3:1b for Ollama

Nothing below this module knows or cares which provider those three values
point at. Stage 4 adds a real timeout and a retry policy around the call
below — the SDK's ten-minute default timeout and automatic retries are
still in effect at this stage.
"""

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "openrouter/free")


def get_client() -> OpenAI:
    """A fresh client per call — same reasoning as auth.get_client(): cheap
    to construct, and it keeps this module free of shared mutable state."""
    return OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)


def complete(messages: list[dict], temperature: float = 0.2) -> str:
    """Calls the model and returns its raw text — the thirty-line
    integration the assignment promises. Low temperature because triage is
    classification, not creative writing: the same task text should get
    the same judgement, not a different one each time."""
    response = get_client().chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=temperature,
    )
    return response.choices[0].message.content or ""
