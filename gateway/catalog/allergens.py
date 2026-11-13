"""GS1 mappings. Ingredient text is never used to infer allergen safety."""

from gateway.catalog.models import AllergenCode, AllergenEntry, Allergens
from gateway.catalog.sanitize import classifications as clean_classifications

GS1_ALLERGENS: dict[str, AllergenCode] = {
    "AM": "milk",
    "ML": "lactose",
    "AW": "gluten",
    "UW": "wheat",
    "AE": "egg",
    "AF": "fish",
    "AC": "crustaceans",
    "UM": "molluscs",
    "AP": "peanuts",
    "AN": "tree_nuts",
    "AY": "soy",
    "AS": "sesame",
    "BC": "celery",
    "BM": "mustard",
    "AU": "sulphites",
    "NL": "lupin",
}
CEREALS = {"UW": "wheat", "GB": "barley", "NR": "rye", "GO": "oats", "ST": "spelt"}
TREE_NUTS = {
    "SA": "almond",
    "SH": "hazelnut",
    "SW": "walnut",
    "SC": "cashew",
    "SP": "pecan",
    "SR": "brazil_nut",
    "SM": "macadamia",
}
STATUSES = {
    "CONTAINS": "contains",
    "MAY_CONTAIN": "may_contain",
    "UNDECLARED": "undeclared",
    "FREE_FROM": "free_from",
}


def extract_allergens(classifications: object) -> Allergens:
    result = Allergens()
    buckets: dict[str, set[AllergenCode]] = {status: set() for status in STATUSES.values()}
    evidence: set[tuple[str, str]] = set()
    for group in clean_classifications(classifications):
        if group["name"] not in {"allergen", "nonAllergen"}:
            continue
        for entry in group["values"]:
            code = entry.get("synkkaId") or entry["id"]
            # Missing status does not establish FREE_FROM, even in nonAllergen.
            evidence.add((code, entry.get("unit") or "UNDECLARED"))
    for code, status in sorted(evidence):
        result.evidence.append(
            AllergenEntry(
                code=code, status=status, specific=TREE_NUTS.get(code) or CEREALS.get(code)
            )
        )
        if code not in GS1_ALLERGENS and code not in TREE_NUTS and code not in CEREALS:
            result.unknown_codes.append(code)
        if status not in STATUSES:
            result.unknown_statuses.append(status)
            continue
        codes: set[AllergenCode] = set()
        if code in GS1_ALLERGENS:
            codes.add(GS1_ALLERGENS[code])
        # Presence of a subtype implies the parent; absence of one subtype does not.
        if status != "FREE_FROM":
            if code in TREE_NUTS:
                codes.add("tree_nuts")
            if code in CEREALS:
                codes.add("gluten")
        buckets[STATUSES[status]].update(codes)
    result.contains = sorted(buckets["contains"])
    result.may_contain = sorted(buckets["may_contain"])
    result.undeclared = sorted(buckets["undeclared"])
    result.free_from = sorted(buckets["free_from"])
    result.data_available = bool(evidence)
    result.unknown_codes = sorted(set(result.unknown_codes))
    result.unknown_statuses = sorted(set(result.unknown_statuses))
    return result
