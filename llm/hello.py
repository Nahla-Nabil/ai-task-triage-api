"""Stage 0's throwaway connectivity check: proves the three environment
variables (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL) actually reach a model,
independent of the rest of this app — no FastAPI, no database, no schema.

    venv/Scripts/python -m llm.hello        (from the repo root, with .env loaded)

Swapping providers (OpenRouter <-> Ollama <-> anything else that speaks the
same API shape) is changing these three values and nothing else — that's
the whole reason the client lives behind llm/client.py instead of being
called ad hoc from routes.
"""

from llm.client import LLM_BASE_URL, LLM_MODEL, get_client


def main() -> None:
    client = get_client()
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": "Reply with exactly the word: ready"}],
    )
    print(f"provider: {LLM_BASE_URL}  model: {LLM_MODEL}")
    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
