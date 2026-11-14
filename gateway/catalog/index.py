"""In-memory token index with explicit, cached file loading."""

import re
import unicodedata
from collections.abc import Iterable
from functools import cache
from pathlib import Path

from pydantic import TypeAdapter

from gateway.catalog.models import SlimRecord


def normalize_token(token: str) -> str:
    if len(token) <= 3 or token in {"glass", "swiss", "asparagus", "couscous", "hummus"}:
        return token
    if token.endswith(("sses", "shes", "ches", "xes", "zes", "oes")):
        return token[:-2]
    return token[:-1] if token.endswith("s") else token


def tokenize(text: str) -> set[str]:
    return {
        normalize_token(token)
        for token in re.findall(r"[^\W_]+", unicodedata.normalize("NFC", text).casefold())
    }


class Catalog:
    def __init__(self, records: Iterable[SlimRecord]) -> None:
        self._records: dict[str, SlimRecord] = {}
        self._names: dict[str, set[str]] = {}
        self._ingredients: dict[str, set[str]] = {}
        self._name_tokens: dict[str, list[set[str]]] = {}
        for record in records:
            code = record.product_id
            if code in self._records:
                raise ValueError(f"Duplicate catalogue product id: {code}")
            self._records[code] = record
            self._name_tokens[code] = [
                tokenize(name) for name in (record.names.fi, record.names.en) if name
            ]
            for token in tokenize(" ".join(filter(None, (record.names.fi, record.names.en)))):
                self._names.setdefault(token, set()).add(code)
            for token in tokenize(
                " ".join(filter(None, record.key_ingredients.model_dump().values()))
            ):
                self._ingredients.setdefault(token, set()).add(code)

    @classmethod
    def load(cls, path: str | Path) -> "Catalog":
        records = TypeAdapter(list[SlimRecord]).validate_json(Path(path).read_bytes())
        return cls(records)

    def __len__(self) -> int:
        return len(self._records)

    def lookup(self, code: str) -> SlimRecord | None:
        return self._records.get(code)

    def search(
        self,
        query: str,
        *,
        exclude_allergens: Iterable[str] = (),
        exclude_sales_blocked: bool = False,
        limit: int = 20,
    ) -> list[SlimRecord]:
        if limit <= 0:
            return []
        tokens = tokenize(query)
        name_hits: dict[str, int] = {}
        ingredient_hits: dict[str, int] = {}
        for token in tokens:
            for code in self._names.get(token, ()):
                name_hits[code] = name_hits.get(code, 0) + 1
            for code in self._ingredients.get(token, set()) - self._names.get(token, set()):
                ingredient_hits[code] = ingredient_hits.get(code, 0) + 1
        excluded = set(exclude_allergens)
        candidates = [
            code
            for code in name_hits.keys() | ingredient_hits.keys()
            if not (excluded & set(self._records[code].allergens.contains))
            and not (exclude_sales_blocked and self._records[code].sales_block)
        ]
        # Each name hit outweighs every possible ingredient-only hit in this query.
        candidates.sort(
            key=lambda code: (
                -(name_hits.get(code, 0) * (len(tokens) + 1) + ingredient_hits.get(code, 0)),
                min(
                    (len(name - tokens) for name in self._name_tokens[code] if name & tokens),
                    default=sum(len(name) for name in self._name_tokens[code]),
                ),
                code,
            )
        )
        return [self._records[code] for code in candidates[:limit]]


@cache
def load_catalog(path: str | Path) -> Catalog:
    """Load once per explicit path. Call load_catalog.cache_clear() to reload in tests/tools."""
    return Catalog.load(path)
