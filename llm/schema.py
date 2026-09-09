"""The output contract for POST /tasks/triage — see JOB-CARD.md. This is the
one place that says what a "valid" triage result looks like; llm/triage.py
validates every model answer against it before anything reaches a caller."""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["work", "personal", "shopping", "health", "other"]
Priority = Literal["low", "normal", "high"]


class TriageRequest(BaseModel):
    """The input contract. Enforced before any model call — see
    llm/triage.py's docstring on why that ordering matters."""

    text: str = Field(min_length=1, max_length=2000)


class TriageResult(BaseModel):
    """The output contract. Every category-like field is a Literal (an enum),
    so a structurally valid JSON object with a category we never allowed is
    still rejected by Pydantic — that's the whole point of Stage 3."""

    category: Category
    priority: Priority
    clean_title: str = Field(min_length=1, max_length=80)
    confidence: float = Field(ge=0.0, le=1.0)
