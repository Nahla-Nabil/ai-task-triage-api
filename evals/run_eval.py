"""Runs evals/cases.json against a live POST /tasks/triage and prints how
many matched — Stage 5. No dependencies beyond the standard library, so
there's nothing new to install just to grade the endpoint.

Usage:
    python evals/run_eval.py                 # against http://localhost:8000
    BASE_URL=https://your-app python evals/run_eval.py

The server must already be running (this makes real HTTP requests — with
LLM_STUB=1 it costs nothing; with a real key, this is 8 of your 50 daily
OpenRouter calls, so budget for roughly two runs a day).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
CASES_PATH = Path(__file__).parent / "cases.json"


def call_triage(text: str) -> dict:
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/tasks/triage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def main() -> None:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    passed = 0
    failures = []

    for case in cases:
        try:
            result = call_triage(case["text"])
        except urllib.error.HTTPError as exc:
            failures.append((case["id"], f"HTTP {exc.code}: {exc.read().decode(errors='replace')}"))
            continue
        except urllib.error.URLError as exc:
            failures.append((case["id"], f"could not reach {BASE_URL}: {exc}"))
            continue

        ok = result.get("category") == case["expected_category"]
        if case.get("expected_low_confidence") and result.get("confidence", 1.0) >= 0.5:
            ok = False

        if ok:
            passed += 1
            print(f"  PASS  {case['id']:<12} -> {result.get('category')} (confidence {result.get('confidence')})")
        else:
            failures.append((case["id"], f"expected {case['expected_category']!r}, got {result}"))
            print(f"  FAIL  {case['id']:<12} -> expected {case['expected_category']!r}, got {result}")

    total = len(cases)
    print(f"\n{passed}/{total} correct on category")
    if failures:
        print("\nFailed cases:")
        for case_id, reason in failures:
            print(f"  - {case_id}: {reason}")


if __name__ == "__main__":
    main()
