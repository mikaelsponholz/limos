"""Write a short quote explanation, checking numeric and date claims against facts."""

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.llm import LLM, LLMOutputError
from gateway.schemas import Quote

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2}|Z)?)?")


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(match.replace(",", ".")) for match in _NUMBER.findall(text)}


@dataclass(frozen=True)
class Explanation:
    text: str
    model: str
    fallback_used: bool


def explain(llm: LLM, quote: Quote, model: str) -> Explanation:
    outcome = {
        "AUTO": "The order can be placed automatically",
        "NEEDS_APPROVAL": "Human approval is required",
        "BLOCK": "The order is blocked",
    }[quote.policy.outcome]
    facts = json.dumps(
        {
            "products": [
                {
                    "name": line.name,
                    "quantity": line.quantity_text,
                    "line_total_eur": line.line_total,
                }
                for line in quote.lines
            ],
            "total_eur": quote.total,
            "delivery_date": quote.delivery_date.isoformat(),
            "order_deadline": quote.order_deadline.isoformat() if quote.order_deadline else None,
            "outcome": outcome,
            "reasons": [rule.description for rule in quote.policy.rules_fired],
        },
        ensure_ascii=False,
    )
    allowed_numbers = _numbers(facts)
    allowed_dates = set(_DATE.findall(facts))

    class QuoteExplanation(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str = Field(min_length=1, max_length=1600)

        @field_validator("text")
        @classmethod
        def check_facts(cls, text: str) -> str:
            sentences = re.split(r"(?<=[.!?])\s+", text.strip())
            if not 2 <= len(sentences) <= 4 or "\n" in text:
                raise ValueError("Use two to four plain sentences.")
            if not _numbers(text) <= allowed_numbers:
                raise ValueError("Unsupported number.")
            for value in _DATE.findall(text):
                if not any(date == value or date.startswith(value + "T") for date in allowed_dates):
                    raise ValueError("Unsupported date.")
            if re.search(r"\b[A-Z]+(?:_[A-Z]+)+\b", text):
                raise ValueError("Do not include rule ids.")
            return text.strip()

    try:
        result = llm.structured(
            facts,
            QuoteExplanation,
            model=model,
            system=(
                "Explain this quote using only these facts in two to four short plain sentences. "
                "Use no jargon or rule ids. Copy quantities, numeric values and ISO dates exactly; "
                "decimal commas are allowed. Do not invent numbers or dates."
            ),
        )
    except LLMOutputError as error:
        products = "; ".join(
            f"{line.name}: {line.quantity_text}, {line.line_total:.2f} EUR" for line in quote.lines
        )
        deadline = (
            f"Order deadline: {quote.order_deadline.isoformat()}"
            if quote.order_deadline
            else "The order deadline is unknown"
        )
        reasons = "; ".join(rule.description.rstrip(".") for rule in quote.policy.rules_fired)
        text = (
            f"{products}. Total: {quote.total:.2f} EUR, "
            f"delivery {quote.delivery_date.isoformat()}. "
            f"{deadline}. {outcome}" + (f": {reasons}" if reasons else "") + "."
        )
        return Explanation(text, error.model_id, True)
    return Explanation(result.value.text, result.model_id, False)
