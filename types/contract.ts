// Limos UI contract. The JSON shapes the API returns and the UI consumes.
// Human-readable reference: docs/CONTRACT.md. Mock data: fixtures/contract/.
// Changing a shape means updating this file, docs/CONTRACT.md, lib/contract/schemas.ts
// and the fixtures in the same change.

// ---------- Primitives ----------

/** Calendar date, YYYY-MM-DD. */
export type ISODate = string;
/** Timestamp with offset, ISO 8601. */
export type ISODateTime = string;
export type Locale = "fi" | "en";

/** Money in euros, two decimals. Net (VAT 0 %) unless the field name says gross. */
export interface Money {
  amount: number;
  currency: "EUR";
}

export type QuantityUnit = "g" | "kg" | "ml" | "l" | "pcs";

export interface Quantity {
  value: number;
  unit: QuantityUnit;
}

// ---------- Event request (UI → API) ----------

export type AllergenCode =
  | "milk"
  | "lactose"
  | "gluten"
  | "wheat"
  | "egg"
  | "fish"
  | "crustaceans"
  | "molluscs"
  | "peanuts"
  | "tree_nuts"
  | "soy"
  | "sesame"
  | "celery"
  | "mustard"
  | "sulphites"
  | "lupin";

export type PreferenceKey =
  | "finnish_origin"
  | "organic"
  | "lactose_free"
  | "gluten_free"
  | "vegetarian"
  | "vegan"
  | "low_carbon";

/** require = must be met or a human approves; prefer = ranks higher. */
export type PreferenceLevel = "require" | "prefer";

export interface Preference {
  key: PreferenceKey;
  level: PreferenceLevel;
}

export type Priority = "price" | "balanced" | "quality";

export interface DishRequest {
  name: string;
  /** Defaults to the guest count. */
  servings?: number;
  notes?: string;
}

export interface EventRequest {
  /** Free text as the buyer wrote it. The agent fills blank fields from it. */
  description: string;
  eventType?: string;
  /** Date of the event. */
  eventDate: ISODate;
  /** Latest acceptable delivery date. Defaults to the day before eventDate. */
  deliveryDate?: ISODate;
  guests: number;
  /** Optional when the description lists the dishes. */
  dishes: DishRequest[];
  preferences: Preference[];
  excludeAllergens: AllergenCode[];
  /** Net budget for the whole order. */
  budget?: Money;
  priority: Priority;
  buyer: { name: string; role?: string };
  locale: Locale;
}

// ---------- Ingredient plan ----------

export interface PlannedIngredient {
  id: string;
  name: string;
  nameFi: string;
  perServing: Quantity;
  /** perServing × servings × (1 + waste factor). */
  total: Quantity;
  /** Preferences that apply to this ingredient (for example lactose_free on dairy). */
  appliesPreferences: PreferenceKey[];
  optional: boolean;
}

export interface PlannedDish {
  id: string;
  name: string;
  servings: number;
  ingredients: PlannedIngredient[];
}

/** The same ingredient merged across dishes. One cart line each. */
export interface AggregatedIngredient {
  id: string;
  name: string;
  nameFi: string;
  total: Quantity;
  usedIn: string[]; // PlannedDish ids
}

export interface IngredientPlan {
  runId: string;
  dishes: PlannedDish[];
  aggregated: AggregatedIngredient[];
  wasteFactor: number; // 0.05 = 5 %
  /** Things the agent assumed, shown to the buyer. */
  assumptions: string[];
  model: string;
}

// ---------- Products and cart ----------

export type ProductLabel =
  | "finnish_label" // Blue Swan, Made in Finland key flag, Finnish heart, Puhtaasti kotimainen
  | "finnish_origin" // country of origin fi
  | "organic_label"
  | "organic_name" // "luomu" in the name only, weaker evidence
  | "lactose_free"
  | "low_lactose"
  | "gluten_free"
  | "vegetarian"
  | "vegan"
  | "msc"
  | "fair_trade"
  | "rainforest_alliance"
  | "nordic_swan";

export interface ProductSummary {
  id: string;
  name: string;
  nameFi: string;
  brand: string | null;
  vendor: string;
  /** ISO 3166-1 alpha-2, lowercase. */
  countryOfOrigin: string | null;
  labels: ProductLabel[];
  allergens: {
    contains: AllergenCode[];
    mayContain: AllergenCode[];
    /** false when the product has no allergen data at all. */
    dataAvailable: boolean;
  };
  /** Content of one sales unit. null when unknown. */
  packSize: Quantity | null;
  salesUnit: { code: string; label: string };
  unitPrice: Money;
  unitPriceGross: Money;
  vatRate: number; // percent
  comparisonPrice: { price: Money; per: "kg" | "l" } | null;
  /** 0 to 1 from Valio's file. null when not in the file. */
  deliveryReliability: number | null;
  /** kg CO2e per kg from Valio's file. null when not in the file. */
  co2PerKg: number | null;
  logisticModel: "stock" | "terminal" | null;
}

export type Tier = "good" | "better" | "best";

export interface ProductOption {
  tier: Tier | null;
  product: ProductSummary;
  /** Whole sales units to order, rounded up to the allowed lot size. */
  salesUnits: number;
  /** What those sales units contain in the ingredient's unit. */
  covers: Quantity;
  lineTotal: Money;
  score: number; // 0 to 100
  matched: PreferenceKey[];
  missed: PreferenceKey[];
  /** Written by code from the score parts. */
  reason: string;
}

export type AvailabilityStatus = "available" | "partial" | "late" | "unavailable" | "unknown";

export interface LineAvailability {
  status: AvailabilityStatus;
  /** Sales units. null when the supplier does not report it (Terminal). */
  availableAmount: number | null;
  availableDate: ISODate | null;
  closingTime: ISODateTime | null;
}

export type ExceptionKind =
  | "partial_stock"
  | "late_delivery"
  | "unavailable"
  | "unmet_preference"
  | "allergen_risk"
  | "missing_data"
  | "substitution"
  | "pack_overbuy"
  | "price_missing"
  | "over_budget";

export interface LineException {
  kind: ExceptionKind;
  severity: "info" | "warning" | "blocking";
  message: string;
}

export type LineDecisionStatus =
  | "auto_approved"
  | "needs_approval"
  | "approved"
  | "rejected"
  | "blocked"
  | "ordered";

export interface LineDecision {
  status: LineDecisionStatus;
  /** Ids from docs/POLICY.md, for example "A2" or "D3". */
  ruleIds: string[];
  reason: string;
}

export interface Substitution {
  originalProductId: string;
  originalName: string;
  source: "valio_substitution_list" | "agent_search";
  reason: string;
}

export interface CartLine {
  id: string;
  ingredientId: string; // AggregatedIngredient id
  ingredientName: string;
  needed: Quantity;
  /** null when nothing orderable was found. */
  chosen: ProductOption | null;
  /** Other tiers and fallbacks, best first. */
  alternatives: ProductOption[];
  /** Extra product when a line is split because of partial stock. */
  split: ProductOption | null;
  availability: LineAvailability;
  exceptions: LineException[];
  substitution: Substitution | null;
  decision: LineDecision;
}

export interface Cart {
  runId: string;
  deliveryDate: ISODate;
  lines: CartLine[];
  totals: {
    net: Money;
    gross: Money;
    /** Sum over lines with carbon data. */
    co2eKg: number | null;
    linesWithCo2: number;
  };
  budget: { limit: Money; status: "within" | "over" | "blocked" } | null;
}

// ---------- Approvals ----------

export interface ApprovalOption {
  id: string;
  label: string;
  /** What happens if chosen, in one sentence. */
  effect: string;
  option: ProductOption | null;
}

export interface ApprovalItem {
  id: string;
  runId: string;
  scope: "line" | "cart";
  lineId: string | null;
  title: string;
  /** Why the rules escalated it. */
  reason: string;
  ruleIds: string[];
  options: ApprovalOption[];
  recommendedOptionId: string | null;
  status: "pending" | "approved" | "rejected";
  createdAt: ISODateTime;
  resolution: {
    optionId: string | null;
    by: string;
    at: ISODateTime;
    note: string | null;
  } | null;
}

/** Body of POST /api/runs/:id/approvals/:approvalId */
export interface ApprovalDecision {
  decision: "approve" | "reject";
  /** Required to approve an item with several options. */
  optionId?: string;
  approver: string;
  note?: string;
}

// ---------- Order ----------

export interface OrderLine {
  lineId: string;
  productId: string;
  name: string;
  salesUnits: number;
  salesUnit: { code: string; label: string };
  lineTotal: Money;
  expectedDelivery: ISODate;
}

export interface OrderResult {
  orderId: string;
  runId: string;
  status: "placed" | "partially_placed" | "not_placed";
  /** true while orders go to the simulated order service (the Valio mock API has no order endpoint). */
  simulated: boolean;
  placedAt: ISODateTime;
  placedBy: { type: "agent" | "human"; name: string };
  /** Why the agent was allowed to place it, in one sentence. */
  authority: string;
  deliveryDate: ISODate;
  lines: OrderLine[];
  excludedLines: { lineId: string; name: string; reason: string }[];
  totals: { net: Money; gross: Money };
}

// ---------- Audit ----------

export type AuditActorType = "agent" | "rules" | "human" | "system";

export type AuditAction =
  | "request_received"
  | "plan_created"
  | "products_searched"
  | "candidates_filtered"
  | "price_checked"
  | "availability_checked"
  | "product_selected"
  | "exception_detected"
  | "substitution_proposed"
  | "rule_applied"
  | "approval_requested"
  | "approval_resolved"
  | "availability_rechecked"
  | "order_placed"
  | "error";

export interface AuditEntry {
  id: string;
  runId: string;
  seq: number;
  at: ISODateTime;
  actor: { type: AuditActorType; name: string; model?: string };
  action: AuditAction;
  /** One plain sentence a buyer can read. */
  summary: string;
  lineId?: string;
  productIds?: string[];
  ruleIds?: string[];
  data?: Record<string, unknown>;
  prevHash: string | null;
  hash: string;
}

// ---------- Run (what GET /api/runs/:id returns) ----------

export type RunStatus =
  | "planning"
  | "sourcing"
  | "awaiting_approval"
  | "ordering"
  | "ordered"
  | "failed";

/** The agent's authority, shown so the buyer knows what it may do alone. */
export interface PolicySummary {
  autoOrderLimit: Money;
  lineAutoLimit: Money;
  budgetHardStopPercent: number; // 15 = budget + 15 %
  maxAutoSubstitutions: number;
  substitutePriceTolerancePercent: number;
  maxOverbuyPercent: number;
  mayContainPolicy: "allow" | "needs_approval" | "block";
}

export interface RunSnapshot {
  id: string;
  status: RunStatus;
  createdAt: ISODateTime;
  updatedAt: ISODateTime;
  request: EventRequest;
  plan: IngredientPlan | null;
  cart: Cart | null;
  approvals: ApprovalItem[];
  order: OrderResult | null;
  policy: PolicySummary;
  audit: AuditEntry[];
  error: string | null;
}

/** Response of POST /api/runs */
export interface CreateRunResponse {
  runId: string;
  status: RunStatus;
}
