"""Slim catalogue facts. Public field names and codes follow the API contract."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AllergenCode = Literal[
    "milk",
    "lactose",
    "gluten",
    "wheat",
    "egg",
    "fish",
    "crustaceans",
    "molluscs",
    "peanuts",
    "tree_nuts",
    "soy",
    "sesame",
    "celery",
    "mustard",
    "sulphites",
    "lupin",
]
ClaimCode = Literal["lactose_free", "low_lactose", "gluten_free", "vegan", "vegetarian"]
LabelCode = Literal[
    "finnish_label",
    "finnish_origin",
    "organic_label",
    "organic_name",
    "lactose_free",
    "low_lactose",
    "gluten_free",
    "vegetarian",
    "vegan",
    "msc",
    "fair_trade",
    "rainforest_alliance",
    "nordic_swan",
]
StorageClass = Literal["ambient", "chilled", "frozen", "unknown"]
PackSource = Literal["unit_conversions", "name", "unknown"]


class CatalogModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", allow_inf_nan=False)


class LocalizedText(CatalogModel):
    fi: str | None = None
    en: str | None = None
    sv: str | None = None


class AllergenEntry(CatalogModel):
    """Original evidence, including specific cereals/nuts and unrecognised codes/statuses."""

    code: str
    status: str
    specific: str | None = None


class Allergens(CatalogModel):
    contains: list[AllergenCode] = Field(default_factory=list)
    may_contain: list[AllergenCode] = Field(default_factory=list)
    undeclared: list[AllergenCode] = Field(default_factory=list)
    free_from: list[AllergenCode] = Field(default_factory=list)
    data_available: bool = False
    evidence: list[AllergenEntry] = Field(default_factory=list)
    unknown_codes: list[str] = Field(default_factory=list)
    unknown_statuses: list[str] = Field(default_factory=list)


class OrganicEvidence(CatalogModel):
    strength: Literal["label", "name_only", "unknown"] = "unknown"
    label_codes: list[str] = Field(default_factory=list)


class Quantity(CatalogModel):
    value: float = Field(gt=0)
    unit: str


class PackSize(CatalogModel):
    value: float = Field(gt=0)
    unit: Literal["kg", "L", "pcs"]


class UnitConversion(CatalogModel):
    packaging_type: str = Field(alias="synkkaPackagingType")
    net_weight: Quantity | None = Field(default=None, alias="netWeight")
    volume: Quantity | None = None


class PackagingUnit(CatalogModel):
    unit_id: str = Field(alias="unitId")
    size_in_base_units: float = Field(gt=0, alias="sizeInBaseUnits")


class Substitution(CatalogModel):
    product: str
    substitute_product: str = Field(alias="substituteProduct")


class SlimRecord(CatalogModel):
    product_id: str = Field(min_length=1)
    names: LocalizedText = Field(default_factory=LocalizedText)
    vendor: str | None = None
    key_ingredients: LocalizedText = Field(default_factory=LocalizedText)
    country_of_origin: str | None = None
    labels: list[LabelCode] = Field(default_factory=list)
    finnish_label_codes: list[str] = Field(default_factory=list)
    organic: OrganicEvidence = Field(default_factory=OrganicEvidence)
    allergens: Allergens = Field(default_factory=Allergens)
    claims: list[ClaimCode] = Field(default_factory=list)
    sales_unit: str | None = None
    base_unit: str | None = None
    units: list[PackagingUnit] = Field(default_factory=list)
    unit_conversions: list[UnitConversion] = Field(default_factory=list)
    allowed_lot_size: float | None = Field(default=None, gt=0, alias="allowedLotSize")
    pack_size: PackSize | None = None
    pack_size_source: PackSource = "unknown"
    variable_unit: bool | None = None
    storage_class: StorageClass = "unknown"
    temperature_condition: int | None = None
    # Absent from the product schema. Availability supplies the authoritative block later.
    sales_block: bool | None = None
    substitutions: list[Substitution] = Field(default_factory=list)
    category: str | None = None
    delivery_reliability: float | None = Field(default=None, ge=0, le=1)
    carbon_kg_co2e_per_kg: float | None = Field(default=None, ge=0)
