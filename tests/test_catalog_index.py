import json
from pathlib import Path

import pytest

from gateway.catalog.index import Catalog, load_catalog, tokenize
from gateway.catalog.models import Allergens, LocalizedText, SlimRecord


def record(code: str, name: str = "", ingredients: str = "", **kwargs: object) -> SlimRecord:
    return SlimRecord.model_validate(
        {
            "product_id": code,
            "names": {"fi": name},
            "key_ingredients": {"fi": ingredients},
            **kwargs,
        }
    )


def test_name_hits_rank_above_ingredients_and_more_name_tokens_rank_first() -> None:
    catalog = Catalog(
        [
            record("TEST-A", ingredients="Keksitty porkkana keitto"),
            record("TEST-B", "Keksitty porkkana"),
            record("TEST-C", "Keksitty porkkana keitto"),
            record("TEST-D", "Keksitty porkkana", "keitto"),
        ]
    )
    assert [r.product_id for r in catalog.search("porkkana keitto")] == [
        "TEST-C",
        "TEST-D",
        "TEST-B",
        "TEST-A",
    ]
    assert [r.product_id for r in catalog.search("porkkana porkkana keitto")] == [
        "TEST-C",
        "TEST-D",
        "TEST-B",
        "TEST-A",
    ]


def test_finnish_characters_case_punctuation_and_unicode_normalization() -> None:
    catalog = Catalog([record("TEST-FI", "Keksitty ÄÄ Öljy Åland")])
    assert tokenize("Ää, ÖLJY! Åland") == {"ää", "öljy", "åland"}
    assert [r.product_id for r in catalog.search("ÄÄ, öljy! åland")] == ["TEST-FI"]
    assert [r.product_id for r in catalog.search("a\u0308a\u0308")] == ["TEST-FI"]
    assert catalog.search("aa") == []


def test_english_names_and_ingredients_are_searchable_but_names_do_not_double_count() -> None:
    english = SlimRecord(product_id="TEST-EN", names=LocalizedText(en="Invented broth"))
    duplicate = record("TEST-Z", "Invented broth", "broth")
    catalog = Catalog([duplicate, english, record("TEST-INGREDIENT", ingredients="broth")])
    assert [r.product_id for r in catalog.search("BROTH")] == [
        "TEST-EN",
        "TEST-Z",
        "TEST-INGREDIENT",
    ]


def test_code_tie_break_and_limit_are_independent_of_input_order() -> None:
    catalog = Catalog(
        [record("TEST-C", "Keksitty"), record("TEST-A", "Keksitty"), record("TEST-B", "Keksitty")]
    )
    assert [r.product_id for r in catalog.search("keksitty", limit=2)] == ["TEST-A", "TEST-B"]
    assert catalog.search("keksitty", limit=0) == []
    assert catalog.search("keksitty", limit=-1) == []
    assert catalog.search("") == []
    assert catalog.search("---") == []
    assert catalog.search("unmatched") == []


def test_hard_filters_before_limit_leave_may_contain_and_missing_for_policy() -> None:
    catalog = Catalog(
        [
            record(
                "TEST-A", "Keksitty", allergens=Allergens(contains=["milk"], data_available=True)
            ),
            record("TEST-B", "Keksitty", sales_block=True),
            record(
                "TEST-C", "Keksitty", allergens=Allergens(may_contain=["milk"], data_available=True)
            ),
            record("TEST-D", "Keksitty"),
            record(
                "TEST-E", "Keksitty", allergens=Allergens(undeclared=["milk"], data_available=True)
            ),
        ]
    )
    assert [r.product_id for r in catalog.search("keksitty")] == [
        "TEST-A",
        "TEST-B",
        "TEST-C",
        "TEST-D",
        "TEST-E",
    ]
    assert [r.product_id for r in catalog.search("keksitty", exclude_allergens=["milk"])] == [
        "TEST-B",
        "TEST-C",
        "TEST-D",
        "TEST-E",
    ]
    assert [r.product_id for r in catalog.search("keksitty", exclude_sales_blocked=True)] == [
        "TEST-A",
        "TEST-C",
        "TEST-D",
        "TEST-E",
    ]
    assert [
        r.product_id
        for r in catalog.search(
            "keksitty", exclude_allergens=["milk"], exclude_sales_blocked=True, limit=1
        )
    ] == ["TEST-C"]


def test_explicit_path_loader_reads_once_per_path_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "catalog.slim.json"
    other_path = tmp_path / "other.slim.json"
    original = record("TEST-CACHE", "Keksitty")
    path.write_text(json.dumps([original.model_dump(mode="json")]), encoding="utf-8")
    other_path.write_text("[]", encoding="utf-8")
    load_catalog.cache_clear()
    try:
        catalog = load_catalog(path)
        assert catalog.lookup("TEST-CACHE") == original
        assert catalog.lookup("TEST-MISSING") is None
        assert len(catalog) == 1
        path.unlink()
        assert load_catalog(path) is catalog  # Cannot read a deleted file a second time.
        assert len(load_catalog(other_path)) == 0
        load_catalog.cache_clear()
        with pytest.raises(FileNotFoundError):
            load_catalog(path)
    finally:
        load_catalog.cache_clear()


def test_duplicate_product_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate catalogue product id"):
        Catalog([record("TEST-DUP"), record("TEST-DUP")])
