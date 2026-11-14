"""Request-wide hard constraints shared by intent, expansion and reorder paths."""

from collections.abc import Sequence
from typing import cast

from gateway.policy.engine import REQUIRE_HARD_DIETARY_KEYS
from gateway.schemas import AllergenCode, Constraint, Preference


def item_constraints(
    preferences: Sequence[Preference],
    exclude_allergens: Sequence[AllergenCode],
    existing: Sequence[Constraint] = (),
) -> list[Constraint]:
    hard: list[Constraint] = [
        p.key for p in preferences if p.level == "require" and p.key in REQUIRE_HARD_DIETARY_KEYS
    ]
    hard.extend(cast(Constraint, f"no_{code}") for code in exclude_allergens)
    return list(dict.fromkeys([*existing, *hard]))
