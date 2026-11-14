# Limos API contract (v0.6)

Contract between the **web** (Johannes) and the **backend** (gateway + Gemini agent, Mikael). v0.2 merged Johannes's v0.1 with Mikael's contract. v0.3 adds customer profiles, reorder, one clarifying question, optional suggestions and per-line delivery and order deadlines (Valio mentor requirements, 2026-11-14). Every earlier field keeps its name and meaning, and new fields are additions. Change it only by agreement, and bump the version.

- Source of truth in code: Pydantic models in `gateway/schemas.py`. The OpenAPI snapshot is `contract/openapi.json`. The web generates its types with `openapi-typescript` from that snapshot.
- Mock data for every shape: `fixtures/api/` (invented products and customers, six-digit product ids starting with `900`).
- Base URL: env `NEXT_PUBLIC_API_BASE` on the web side (for example `http://localhost:8000`).
- JSON, UTF-8, snake_case. Times are ISO 8601 with offset (Helsinki). Dates are `YYYY-MM-DD`.
- Money is EUR, numbers **excl. VAT** unless the field name says `with_tax`.
- CORS allows the web origin.
- Errors: HTTP 4xx / 5xx with `{"error": {"code": "string", "message": "human readable"}}`. Fixture: `error.json`.

Three kinds of endpoints:
- **Buyer side**: the kitchen user starts a request and follows it.
- **Approver side**: a human approves or rejects. Never callable by an agent, and not exposed over MCP.
- **Gateway tools**: what any buying agent calls (our Gemini agent, Claude Desktop over MCP, a plain script).

## Changes from v0.5 to v0.6

- `QuoteLine` in `POST /gateway/quotes` gains optional `substitute_for` (the original product id) and `substitute_source` (`valio` or `agent`). Both default to `null` and are set together. The quoted line then has `is_substitute: true` with the same two fields, and `SUBSTITUTION` fires. A `valio` source must match a substitution in Valio's data, else `422`.
- An item may have two lines in one quote. The second has `is_split: true`, and `SPLIT_DELIVERY` fires. More than two lines for one item is a `422`.

## Changes from v0.4 to v0.5

- `Mandate` gains `always_require_approval: bool`, default `false`. It is enabled for `ab-oy` only.
- New rule id `APPROVAL_ALWAYS_REQUIRED` requires human approval on every quote when the mandate setting is enabled. It follows more specific approval reasons and is never listed on a `BLOCK` quote. Existing approval, hash binding, re-check and ordering behaviour is unchanged.

## Changes from v0.3 to v0.4

- `QuoteBody` gains optional `exclude_allergens` and `constraints`, both defaulting to `[]` and applying to every line. They only add restrictions to the existing context.
- Standalone quotes always apply the union of the buyer's customer-profile allergen exclusions and require-level dietary preferences. Quotes with a `request_id` keep that request's per-item rules; body additions are merged into either context.

## Changes from v0.2

- New `Customer` and `PastOrder` objects, `GET /customers` and `GET /customers/{customer_id}`. Profiles are seeded and `simulated: true`.
- `POST /requests` gains `customer_id` and `reorder_of`. `buyer_id` may be left out when `customer_id` is set.
- Request `status` gains `NEEDS_INPUT`. The request gains `customer_id`, `question` and `suggestions`. New `POST /requests/{request_id}/answer`.
- `intent` gains `based_on` (reorder) and `filled_gaps` (where each missing value came from).
- `Offer` gains `delivery_date`, `order_deadline` and `quantity_text`. `Quote` and `Order` gain `order_deadline`.
- New `Suggestion` object and `POST /requests/{request_id}/suggestions/{suggestion_id}/accept`.
- Request list items gain `customer_id`, `delivery_date` and `order_deadline`. Approval list items gain `order_deadline`. Presets gain `customer_id`.
- `Step.type` gains `CUSTOMER_LOADED`, `CLARIFICATION_REQUESTED`, `CLARIFICATION_ANSWERED`, `SUGGESTIONS_PROPOSED`, `SUGGESTION_ACCEPTED`.

## Changes from v0.1 to v0.2

- `POST /requests` also accepts `preset_id`, `event`, `items`, `preferences`, `exclude_allergens`, `budget` and `priority`. New `GET /presets`.
- `intent` gains `event`, `dishes`, `preferences`, `exclude_allergens`, `budget`, `priority` and `assumptions`. Items gain `item_id`, `description_fi`, `used_in`.
- `Offer` gains `item_id`, `tier`, `score`, `reason`, `country_of_origin`, `labels`, `allergens`, `pack_size`, `quantity_covered`, `is_split`. `dietary_check` covers more keys. More `flags`.
- `Quote` gains `request_id`, `buyer_id`, `agent_id`, `delivery_date`, `co2e_total_kg`, `budget`, `option_label`.
- `PolicyDecision.rules_fired[]` gains optional `item_id` and `product_id`. `mandate` gains more limits.
- `order` becomes the full `Order` object, with `simulated` and `authority`.
- `Step` gains `model`, `prev_hash` and `hash`. More `type` values.
- `GET /audit/{id}` adds `chain_valid`.
- New gateway tool endpoints under `/gateway/...`.

---

## Shared objects

### Codes

- **Preference keys**: `lactose_free`, `gluten_free`, `finnish_origin` (kotimainen), `organic`, `vegetarian`, `vegan`, `low_carbon`. Levels: `require` (if unmet, a human decides) or `prefer` (ranks higher).
- **Allergen codes**: `milk`, `lactose`, `gluten`, `wheat`, `egg`, `fish`, `crustaceans`, `molluscs`, `peanuts`, `tree_nuts`, `soy`, `sesame`, `celery`, `mustard`, `sulphites`, `lupin`.
- **Labels**: `finnish_label` (Blue Swan, key flag, Finnish heart), `finnish_origin` (country of origin fi), `organic_label` (certified), `organic_name` ("luomu" only in the name, weaker), `lactose_free`, `low_lactose`, `gluten_free`, `vegetarian`, `vegan`, `msc`, `fair_trade`, `rainforest_alliance`, `nordic_swan`.
- **Units** in quantities: `g`, `kg`, `ml`, `L`, `pcs`.

### `Offer` (one candidate product for one item)

```json
{
  "product_id": "900201",
  "item_id": "it_cream",
  "name": "Example Dairy whipping cream 2 l lactose free",
  "vendor": "Example Dairy Oy",
  "sales_unit": "PRK",
  "pack_description": "2 l carton",
  "pack_size": {"value": 2, "unit": "L"},
  "quantity_sales_units": 2,
  "quantity_requested": {"value": 6.3, "unit": "L"},
  "quantity_covered": {"value": 4, "unit": "L"},
  "unit_price": 7.9,
  "list_price": 8.6,
  "comparison_price": {"value": 3.95, "unit": "L"},
  "line_total": 15.8,
  "line_total_with_tax": 17.93,
  "tax_rate": 13.5,
  "available_amount": 2,
  "quantity_text": "2 x 2 l carton",
  "available_date": "2026-11-16",
  "requested_date": "2026-11-20",
  "delivery_date": "2026-11-20",
  "closing_time": "2026-11-19T17:00:00+02:00",
  "order_deadline": "2026-11-19T17:00:00+02:00",
  "logistic_model": "Stock",
  "delivery_reliability": 0.98,
  "carbon_kg_co2e_per_kg": 4.5,
  "country_of_origin": "fi",
  "labels": ["finnish_label", "finnish_origin", "lactose_free"],
  "allergens": {"contains": ["milk"], "may_contain": [], "data_available": true},
  "dietary_check": {"lactose_free": "confirmed", "finnish_origin": "confirmed", "organic": "violated"},
  "tier": "better",
  "score": 88,
  "reason": "Lactose-free claim, Finnish (Blue Swan label), 3.95 EUR/l. Only 2 cartons in stock.",
  "is_split": false,
  "is_substitute": false,
  "substitute_for": null,
  "substitute_source": null,
  "flags": ["PARTIAL"]
}
```

- `quantity_requested` is what the item needs. `quantity_covered` is what `quantity_sales_units` contain. `pack_size` is `null` when unknown.
- `quantity_text` is the plain shopping-list quantity composed by code from `quantity_sales_units` and the pack ("2 x 2 l carton", "3 x 1 kg"). Falls back to "2 PRK" when the pack size is unknown.
- `delivery_date` is the day this line arrives if ordered now: the later of `requested_date` and `available_date`. `null` when not deliverable. It differs from `requested_date` only for a late line (flag `LATE_DELIVERY`).
- `order_deadline` is the time by which the order must be placed for `delivery_date`: Valio's `closing_time` when it is in the future, else `null` with flag `CUTOFF_PASSED` (Terminal data shows past cut-offs with valid dates). `closing_time` stays the raw Valio value.
- `available_amount` is in sales units: `null` for Terminal products (amount unknown), the real number for Stock, and `null` for "ample" sentinels. `available_date` is `null` when not deliverable.
- `delivery_reliability` and `carbon_kg_co2e_per_kg` are `null` when Valio's files have no value. Never a guess.
- `allergens.data_available` is `false` when the product has no allergen data. Missing data is never shown as safe.
- `dietary_check` has one key per preference and per excluded allergen (`no_<allergen>`, for example `no_tree_nuts`). Values: `confirmed`, `violated`, `unknown` (data missing).
- `tier`: `good`, `better`, `best` or `null` (fallback or split product). The UI shows the three tiers per item from `candidates`.
- `is_split`: `true` for the second product that covers the rest of a short item.
- `substitute_source`: `valio` (Valio-approved substitution), `agent` (proposed by the AI) or `null`. `substitute_for` is the original product id.
- `flags`: `LATE_DELIVERY`, `PARTIAL`, `NOT_DELIVERABLE`, `SALES_BLOCK`, `PRICE_MISSING`, `CUTOFF_PASSED`, `TERMINAL_LOW_RELIABILITY`, `MAY_CONTAIN_EXCLUDED`, `ALLERGEN_DATA_MISSING`, `PACK_SIZE_UNKNOWN`, `OVERBUY`.

### `PolicyDecision`

```json
{
  "outcome": "NEEDS_APPROVAL",
  "rules_fired": [
    {"rule_id": "SPLIT_DELIVERY", "description": "Whipping cream is split across two products because only 4 of 6.3 l are in stock.", "outcome": "NEEDS_APPROVAL", "item_id": "it_cream", "product_id": "900202"}
  ],
  "mandate": {"buyer_id": "demo-catering", "auto_limit": 1500, "line_limit": 250, "hard_cap": 5000, "max_overbuy_percent": 25, "waste_percent": 5, "always_require_approval": false}
}
```

`outcome`: `AUTO`, `NEEDS_APPROVAL`, `BLOCK`. The strictest fired rule decides. `rules_fired` is empty for `AUTO`. Rule ids and meanings: `docs/PLAN.md` section 4 (later `docs/POLICY.md`). Fixture: `policy-decision.json`.

The `Mandate` object contains the buyer id, limits and `always_require_approval: bool` (default `false`). When enabled, `APPROVAL_ALWAYS_REQUIRED` fires with `NEEDS_APPROVAL` and the description "This buyer approves every order personally." It is listed after more specific approval rules and omitted if any rule blocks the quote. Policy reads only the mandate, never customer profiles or history.

### `Quote`

```json
{
  "quote_id": "q_wed_a",
  "request_id": "r_wed01",
  "buyer_id": "demo-catering",
  "agent_id": "limos-gemini",
  "status": "PENDING_APPROVAL",
  "option_label": "Split the cream, use Valio's substitute for cucumber",
  "delivery_date": "2026-11-20",
  "order_deadline": "2026-11-19T17:00:00+02:00",
  "lines": [ /* Offer */ ],
  "total": 82.07,
  "total_with_tax": 93.15,
  "co2e_total_kg": 60.9,
  "budget": {"limit": 900, "status": "within"},
  "created_at": "2026-11-14T09:12:41+02:00",
  "expires_at": "2026-11-14T09:42:41+02:00",
  "content_hash": "sha256:...",
  "policy": { /* PolicyDecision */ },
  "agent_explanation": "..."
}
```

- `lines` are the chosen offers, one or two per item (two when split). They are the shopping list: product, `quantity_text`, `unit_price`, `line_total`, `delivery_date` and `order_deadline` per line, with `total` below.
- `delivery_date` is the latest line `delivery_date` (one order, one delivery). `order_deadline` is the earliest line `order_deadline`, or `null` when no line has one. The UI shows both at the top of the list, not in the details.
- `status`: `DRAFT`, `AUTO_APPROVED`, `PENDING_APPROVAL`, `APPROVED`, `REJECTED`, `BLOCKED`, `EXPIRED`, `ORDERED`.
- `expires_at` is 30 min after creation, never past a future cut-off.
- `content_hash` covers lines, totals, delivery date and expiry. Any change means a new hash.
- `budget`: `null` without a budget. Status `within`, `over` (approval) or `hard_stop` (blocked).
- `co2e_total_kg`: sum over lines with carbon data, `null` if none.
- `option_label`: short name when the quote is one of several approval options, else `null`.

Fixture: `quote.json`.

### `Order`

```json
{
  "order_id": "o_wed01",
  "quote_id": "q_wed_a",
  "request_id": "r_wed01",
  "status": "CONFIRMED",
  "placed_at": "2026-11-14T09:20:05+02:00",
  "placed_by": {"type": "agent", "name": "limos-gemini"},
  "approved_by": "Anna Example",
  "simulated": true,
  "authority": "Anna Example approved quote q_wed_a and its content hash matched. The total of 82.07 EUR is under the 1 500 EUR auto limit and the 900 EUR budget.",
  "idempotency_key": "r_wed01-q_wed_a",
  "delivery_date": "2026-11-20",
  "order_deadline": "2026-11-19T17:00:00+02:00",
  "lines": [ /* Offer */ ],
  "total": 82.07,
  "total_with_tax": 93.15
}
```

- `status`: `CONFIRMED`, `FAILED`.
- `simulated` is `true` while orders go to Limos's simulated order store (the Valio mock API has no order endpoint). The UI shows it plainly.
- `authority`: one sentence on why the order was allowed. `approved_by` is `null` for `AUTO`.

Fixture: `order.json`.

### `Step` (one entry in the agent timeline and decision log)

```json
{
  "seq": 6,
  "at": "2026-11-14T09:12:28+02:00",
  "actor": "gateway",
  "type": "TOOL_CALL",
  "title": "Checked price and availability for 11 products for 20 November",
  "detail": {"tool": "check_offers", "product_ids": ["900101", "900201"]},
  "model": null,
  "prev_hash": "sha256:...",
  "hash": "sha256:..."
}
```

- `actor`: `user`, `agent`, `gateway`, `policy`, `human`.
- `type`: `REQUEST_RECEIVED`, `CUSTOMER_LOADED`, `INTENT_PARSED`, `CLARIFICATION_REQUESTED`, `CLARIFICATION_ANSWERED`, `SUGGESTIONS_PROPOSED`, `SUGGESTION_ACCEPTED`, `EVENT_EXPANDED`, `TOOL_CALL`, `CANDIDATES`, `CHOICE`, `EXCEPTION`, `SUBSTITUTE_PROPOSED`, `QUOTE_CREATED`, `POLICY_DECISION`, `APPROVAL_REQUESTED`, `APPROVED`, `REJECTED`, `QUOTE_EXPIRED`, `REQUOTED`, `AVAILABILITY_RECHECKED`, `ORDER_PLACED`, `ERROR`.
- `title` is one plain sentence and is what the timeline shows.
- `model` is set for steps where Gemini produced the result.
- `hash` = SHA-256 over the step including `prev_hash`. The first step has `prev_hash: null`.

Fixture: `steps.json`.

### `Customer` (seeded demo profile)

```json
{
  "customer_id": "c_pena",
  "name": "Frank",
  "company": "Ab Oy",
  "role": "Executive",
  "buyer_id": "ab-oy",
  "age_group": "55_plus",
  "playful": false,
  "preferences": [{"key": "finnish_origin", "level": "prefer"}],
  "exclude_allergens": [],
  "defaults": {"priority": "quality", "typical_guests": 30, "budget": null},
  "order_history": [ /* PastOrder, newest first */ ],
  "simulated": true
}
```

- Profiles are seeded demo data, clearly invented and marked `simulated: true`. There is no sign-in: choosing a customer in the UI is how the user is recognised.
- `buyer_id` is the mandate the customer buys under (limits and allowed agents).
- `age_group`: `under_35`, `35_to_54`, `55_plus`. Profile information only; no rule or ranking uses it.
- `playful`: `true` means the customer sees optional suggestions (`Suggestion`). `false` means none are generated.
- `preferences`, `exclude_allergens` and `defaults` fill gaps in a request. Anything the request sets wins.
- `defaults.priority` is `price`, `balanced` or `quality`. `typical_guests` and `budget` may be `null`.

Fixtures: `customer.json` (Frank, with history), `customers.json` (list).

### `PastOrder` (one entry in `order_history`)

```json
{
  "order_ref": "ph_pena_01",
  "date": "2026-10-02",
  "title": "Layer cakes for the board meeting",
  "event_type": "company_meeting",
  "guests": 30,
  "lines": [{"product_id": "900301", "name": "Example Bakery layer cake 24 portions", "quantity_sales_units": 2, "sales_unit": "ST", "quantity_text": "2 x 24 portions", "unit_price": 38.5, "line_total": 77.0}],
  "total": 77.0,
  "source": "seeded",
  "simulated": true
}
```

- `source`: `seeded` (demo history) or `limos` (an order placed through Limos for this customer, appended when it is placed). Both are simulated.
- `guests` may be `null` for item-list orders. Reorder scaling uses it.
- History exists only for seeded customers. No policy rule uses it.

### `Question` (the one clarifying question)

```json
{
  "question_id": "qn_r_lunch02",
  "field": "guests",
  "text": "How many people is the lunch for?",
  "choices": ["30", "60", "80"],
  "default": "30",
  "asked_at": "2026-11-14T10:02:11+02:00"
}
```

- At most one question per request, asked only when a needed value (`guests`, `deliver_by` or what to order) can't be filled from the text, the form, the history or the profile defaults.
- `field`: `guests`, `deliver_by`, `items`, `event_type` or `other`. `choices` may be empty for free text. `default` is what Limos uses when the user taps "Use default", and may be `null`.

### `Suggestion` (optional "if you want" line)

```json
{
  "suggestion_id": "sg_r_chef01_1",
  "kind": "seasonal",
  "title": "Swap the strawberries for lingonberries, in season now",
  "reason": "Lingonberries are in season in November and cost less per kg in this order.",
  "replaces_item_id": "it_berries",
  "offer": { /* Offer or null */ },
  "status": "OFFERED"
}
```

- `kind`: `seasonal`, `holiday` or `trend`. `status`: `OFFERED`, `ACCEPTED`, `DECLINED`.
- Suggestions are never in the quote and never ordered unless the user accepts one. Accepting creates a new quote, so the rules run again.
- Only for customers with `playful: true`. For everyone else `suggestions` is `[]`.
- `offer` is a priced line from the gateway when the suggestion is a product, else `null`. `replaces_item_id` is `null` for an addition.

---

## Buyer side

### `GET /customers`

The seeded profiles for the customer picker: `[{"customer_id", "name", "company", "role", "age_group", "playful", "last_order": {"order_ref", "date", "title"} | null, "simulated": true}]`. Fixture: `customers.json`.

### `GET /customers/{customer_id}`

`Customer` with `order_history`. `404 CUSTOMER_NOT_FOUND` for an unknown id. Fixture: `customer.json`.

### `GET /presets`

The demo presets as ready-filled request bodies.

```json
[{"preset_id": "wedding_120", "title": "Wedding for 120", "description": "Cake, sandwiches and a salad. Lactose-free, Finnish where possible.", "expected": "Partial stock and a Valio-approved substitute go to approval.", "request": { /* POST /requests body */ }}]
```

Presets: `restaurant_weekly`, `wedding_120`, `birthday_kids_25`, `company_lunch_60`. Each preset also has `customer_id` (`null` when it isn't tied to a seeded customer). Fixture: `presets.json`.

### `POST /requests`

Start a request. At least one of `preset_id`, `text`, `event`, `items` or `reorder_of` must be set. Fields set here win over what the agent reads from `text`, which wins over the customer's history and profile defaults.

```json
{
  "buyer_id": "demo-catering",
  "customer_id": null,
  "reorder_of": null,
  "preset_id": null,
  "text": "Wedding for 120 guests on Saturday 21 November...",
  "requested_date": "2026-11-20",
  "event": {"type": "wedding", "date": "2026-11-21", "guests": 120, "dishes": [{"name": "Wedding cake", "servings": null}]},
  "items": [],
  "preferences": [{"key": "lactose_free", "level": "require"}],
  "exclude_allergens": [],
  "budget": 900,
  "priority": "balanced"
}
```

- `customer_id`: the chosen seeded customer. Loads their preferences, defaults and history. When set, `buyer_id` may be omitted and comes from the customer.
- `reorder_of`: a `PastOrder.order_ref` of that customer, for "same as last time". With `event.guests` set, code scales the past quantities to the new guest count. Free text such as "same as last time but for 80 people" reaches the same result through `text`.
- `text` may be short and vague ("cakes for Friday") or long and clear. Gaps are filled from history and defaults and listed in `intent.filled_gaps`.
- `requested_date` is the latest acceptable delivery date (`deliver_by`).
- `items[]`: `{"description", "quantity", "unit", "constraints"}` for item-list requests (`restaurant_weekly`).
- `priority`: `price`, `balanced` or `quality`. Picks the default tier.
- Stretch: `multipart/form-data` with the same fields plus `image` (jpg / png).

Response `202`: `{"request_id": "r_wed01", "status": "RUNNING"}`. Fixtures: `request-create.json`, `request-created.json`.

### `GET /requests/{request_id}`

Poll every 1 to 2 s while `status` is `RUNNING`.

```json
{
  "request_id": "r_wed01",
  "buyer_id": "demo-catering",
  "customer_id": null,
  "preset_id": "wedding_120",
  "status": "WAITING_APPROVAL",
  "created_at": "...",
  "input": {"text": "...", "image_url": null},
  "intent": {
    "event": {"type": "wedding", "date": "2026-11-21", "guests": 120},
    "dishes": [{"dish_id": "d_cake", "name": "Wedding cake", "servings": 120}],
    "items": [{"item_id": "it_cream", "description": "whipping cream", "description_fi": "kuohukerma", "quantity": 6.3, "unit": "L", "constraints": ["lactose_free"], "used_in": [{"dish_id": "d_cake", "quantity": 6.3}]}],
    "deliver_by": "2026-11-20",
    "preferences": [{"key": "lactose_free", "level": "require"}],
    "exclude_allergens": [],
    "budget": 900,
    "priority": "balanced",
    "assumptions": ["5 % waste added to every item."],
    "based_on": null,
    "filled_gaps": []
  },
  "question": null,
  "steps": [ /* Step */ ],
  "candidates": [ /* Offer, all items, grouped by item_id */ ],
  "quote": { /* Quote or null: the recommended one */ },
  "suggestions": [ /* Suggestion */ ],
  "approval_id": "a_wed01",
  "order": { /* Order or null */ },
  "error": null
}
```

- Request `status`: `RUNNING`, `NEEDS_INPUT`, `WAITING_APPROVAL`, `ORDERED`, `BLOCKED`, `REJECTED`, `FAILED`.
- `NEEDS_INPUT`: `question` is set and the run waits for `POST /requests/{request_id}/answer`. `question` is `null` otherwise. After one answer the agent never asks again; remaining gaps get defaults.
- `intent.based_on`: `null`, or for a reorder `{"order_ref", "title", "date", "guests_before", "guests_now", "scale"}` (`scale` = `guests_now / guests_before`, `1` when either is `null`).
- `intent.filled_gaps[]`: `{"field", "value", "source", "note"}` with `source` `text`, `form`, `history`, `profile`, `default` or `answer`. The UI shows these as "We assumed ..." lines the user can correct.
- `suggestions`: `[]` unless the customer is playful. Shown below the shopping list as separate "if you want" lines.
- `intent.event` and `intent.dishes` are `null` / `[]` for item-list requests. `items[].constraints` are the preference keys and `no_<allergen>` checks that apply to that item.
- `approval_id` is set while an approval exists.
- `error` is `{"code", "message"}` when `FAILED`.

Fixtures: `request.json`, `request-needs-input.json`.

### `POST /requests/{request_id}/answer`

Body: `{"question_id": "qn_r_lunch02", "answer": "60"}`. `answer` may be `null` to use the question's `default`. Response `202`: `{"request_id", "status": "RUNNING"}`. `409 NO_OPEN_QUESTION` when the request isn't `NEEDS_INPUT` or the id doesn't match. Fixture: `answer-body.json`.

### `POST /requests/{request_id}/suggestions/{suggestion_id}/accept`

No body. The agent adds or swaps the line and requests a new quote, so the rules run again and a new `content_hash` and approval may follow. Response `202`: `{"request_id", "status": "RUNNING"}`. `409 SUGGESTION_NOT_OFFERED` when it isn't `OFFERED`. Declining needs no call: the line is simply not accepted.

### `GET /requests?buyer_id=...` or `?customer_id=...`

Recent requests: `[{"request_id", "customer_id", "created_at", "title", "status", "total", "delivery_date", "order_deadline"}]`. `title` is a short summary ("Wedding for 120, 21 Nov"). `delivery_date` and `order_deadline` come from the current quote and are `null` without one. Fixture: `request-list.json`.

---

## Approver side (human only)

### `GET /approvals?status=PENDING`

```json
[{"approval_id": "a_wed01", "request_id": "r_wed01", "quote_id": "q_wed_a", "buyer_id": "demo-catering", "created_at": "...", "summary": "Wedding for 120, 93.15 EUR incl. VAT, delivery 20 Nov", "reasons": ["SPLIT_DELIVERY", "SUBSTITUTION"], "order_deadline": "2026-11-19T17:00:00+02:00", "expires_at": "..."}]
```

Fixture: `approval-list.json`.

### `GET /approvals/{approval_id}`

```json
{"approval_id": "a_wed01", "status": "PENDING", "quote": { /* Quote, recommended */ }, "request": {"request_id": "r_wed01", "title": "...", "intent": { /* intent */ }}, "options": [ /* Quote, ... */ ], "decided_by": null, "decided_at": null, "comment": null}
```

- `status`: `PENDING`, `APPROVED`, `REJECTED`, `EXPIRED`.
- `options` holds alternative quotes (at most 3, the recommended one first) when the agent offers choices, for example "split the cream" or "1 l cartons only". Empty otherwise.

Fixture: `approval-detail.json`.

### `POST /approvals/{approval_id}/approve`

Body: `{"approver": "Anna Example", "quote_id": "q_wed_a", "content_hash": "sha256:...", "comment": "OK"}`

- One-tap approval: the UI fills the body from the recommended quote it shows (`quote_id`, `content_hash`) and the loaded customer's name as `approver`, so a single "Approve order" tap sends it. Choosing another option is a second, smaller action.
- `quote_id` selects the option. `content_hash` must match that quote now: `409` if it changed after it was shown (`{"error": {"code": "QUOTE_CHANGED", ...}}`). `410` with `QUOTE_EXPIRED` if it expired; the agent re-quotes and a new approval appears.

Response: `{"status": "APPROVED", "order": { /* Order */ }}`. Fixtures: `approve-body.json`, `approve-response.json`.

### `POST /approvals/{approval_id}/reject`

Body: `{"approver": "Anna Example", "reason": "Too expensive"}` → `{"status": "REJECTED"}`

### `GET /audit/{request_id}`

`{"request_id": "r_wed01", "chain_valid": true, "steps": [ /* Step */ ]}`. Fixture: `audit.json`.

---

## Gateway tools (agents: Gemini, MCP, scripts)

Every call carries `buyer_id` and `agent_id`. The gateway checks the agent against the buyer's mandate. The same tools are exposed over MCP with the same names (`mcp_server/`). There is no approve tool.

| Endpoint | MCP tool | Body | Returns |
|---|---|---|---|
| `POST /gateway/search` | `search_products` | `{query, constraints, exclude_allergens, limit}` | `{"products": [{product_id, name, vendor, sales_unit, pack_size, country_of_origin, labels, allergens}]}` from the local catalogue, never the Valio API |
| `POST /gateway/offers` | `check_offers` | `{product_ids, item_id, quantity, unit, requested_date, constraints, exclude_allergens}` | `{"offers": [Offer]}`. Calls Valio price and availability for this shortlist only |
| `POST /gateway/quotes` | `request_quote` | `{request_id?, buyer_id, agent_id, requested_date, lines: [{item_id, product_id, quantity_sales_units, substitute_for?, substitute_source?}], budget?, option_label?, exclude_allergens?: [], constraints?: []}` | `Quote` with the policy decision |
| `POST /gateway/quotes/{quote_id}/request-approval` | `request_approval` | `{agent_id, explanation, alternative_quote_ids?}` | `{"approval_id", "status": "PENDING"}` |
| `POST /gateway/orders` | `place_order` | `{quote_id, agent_id, idempotency_key}` | `Order`. `403 NEEDS_APPROVAL` without a matching approval, `410 QUOTE_EXPIRED`, `422 BLOCKED`. Same key → same order |
| `GET /gateway/orders/{order_id}` | `get_order_status` | | `Order`, or the pending approval state |

`QuoteBody.exclude_allergens` uses the allergen codes above. `QuoteBody.constraints` accepts preference keys and `no_<allergen>` values (for example `lactose_free` or `no_milk`). Both lists default to `[]` and are merged without duplicates, preserving order, into every line's rules. Standalone profile rules include only this buyer's customers, including profile overlays, and only require-level `lactose_free`, `gluten_free`, `vegetarian` and `vegan` preferences. Empty lists cannot remove those rules. Fired rules appear in the existing `policy.rules_fired` descriptions.

## Openness

- `GET /openapi.json`: the backend's generated spec. The web links to it as "Open spec".
- `GET /health`: `{"status": "ok", "valio_api": "ok", "database": "ok"}`. Fixture: `health.json`.
