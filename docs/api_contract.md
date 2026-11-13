# Limos API contract (v0.2)

Contract between the **web** (Johannes) and the **backend** (gateway + Gemini agent, Mikael). v0.2 merges Johannes's v0.1 with Mikael's contract: every v0.1 field keeps its name and meaning, and new fields are additions. Change it only by agreement, and bump the version.

- Source of truth in code: Pydantic models in `gateway/schemas.py`. The OpenAPI snapshot is `contract/openapi.json`. The web generates its types with `openapi-typescript` from that snapshot.
- Mock data for every shape: `fixtures/api/` (invented products, ids `9000xx`).
- Base URL: env `NEXT_PUBLIC_API_BASE` on the web side (for example `http://localhost:8000`).
- JSON, UTF-8, snake_case. Times are ISO 8601 with offset (Helsinki). Dates are `YYYY-MM-DD`.
- Money is EUR, numbers **excl. VAT** unless the field name says `with_tax`.
- CORS allows the web origin.
- Errors: HTTP 4xx / 5xx with `{"error": {"code": "string", "message": "human readable"}}`. Fixture: `error.json`.

Three kinds of endpoints:
- **Buyer side**: the kitchen user starts a request and follows it.
- **Approver side**: a human approves or rejects. Never callable by an agent, and not exposed over MCP.
- **Gateway tools**: what any buying agent calls (our Gemini agent, Claude Desktop over MCP, a plain script).

## Changes from v0.1

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
  "available_date": "2026-11-16",
  "requested_date": "2026-11-20",
  "closing_time": "2026-11-19T17:00:00+02:00",
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
  "mandate": {"buyer_id": "demo-catering", "auto_limit": 1500, "line_limit": 250, "hard_cap": 5000, "max_overbuy_percent": 25, "waste_percent": 5}
}
```

`outcome`: `AUTO`, `NEEDS_APPROVAL`, `BLOCK`. The strictest fired rule decides. `rules_fired` is empty for `AUTO`. Rule ids and meanings: `docs/PLAN.md` section 4 (later `docs/POLICY.md`). Fixture: `policy-decision.json`.

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

- `lines` are the chosen offers, one or two per item (two when split).
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
- `type`: `REQUEST_RECEIVED`, `INTENT_PARSED`, `EVENT_EXPANDED`, `TOOL_CALL`, `CANDIDATES`, `CHOICE`, `EXCEPTION`, `SUBSTITUTE_PROPOSED`, `QUOTE_CREATED`, `POLICY_DECISION`, `APPROVAL_REQUESTED`, `APPROVED`, `REJECTED`, `QUOTE_EXPIRED`, `REQUOTED`, `AVAILABILITY_RECHECKED`, `ORDER_PLACED`, `ERROR`.
- `title` is one plain sentence and is what the timeline shows.
- `model` is set for steps where Gemini produced the result.
- `hash` = SHA-256 over the step including `prev_hash`. The first step has `prev_hash: null`.

Fixture: `steps.json`.

---

## Buyer side

### `GET /presets`

The demo presets as ready-filled request bodies.

```json
[{"preset_id": "wedding_120", "title": "Wedding for 120", "description": "Cake, sandwiches and a salad. Lactose-free, Finnish where possible.", "expected": "Partial stock and a Valio-approved substitute go to approval.", "request": { /* POST /requests body */ }}]
```

Presets: `restaurant_weekly`, `wedding_120`, `birthday_kids_25`, `company_lunch_60`. Fixture: `presets.json`.

### `POST /requests`

Start a request. At least one of `preset_id`, `text`, `event` or `items` must be set. Fields set here win over what the agent reads from `text`.

```json
{
  "buyer_id": "demo-catering",
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
    "assumptions": ["5 % waste added to every item."]
  },
  "steps": [ /* Step */ ],
  "candidates": [ /* Offer, all items, grouped by item_id */ ],
  "quote": { /* Quote or null: the recommended one */ },
  "approval_id": "a_wed01",
  "order": { /* Order or null */ },
  "error": null
}
```

- Request `status`: `RUNNING`, `WAITING_APPROVAL`, `ORDERED`, `BLOCKED`, `REJECTED`, `FAILED`.
- `intent.event` and `intent.dishes` are `null` / `[]` for item-list requests. `items[].constraints` are the preference keys and `no_<allergen>` checks that apply to that item.
- `approval_id` is set while an approval exists.
- `error` is `{"code", "message"}` when `FAILED`.

Fixture: `request.json`.

### `GET /requests?buyer_id=...`

Recent requests: `[{"request_id", "created_at", "title", "status", "total"}]`. `title` is a short summary ("Wedding for 120, 21 Nov"). Fixture: `request-list.json`.

---

## Approver side (human only)

### `GET /approvals?status=PENDING`

```json
[{"approval_id": "a_wed01", "request_id": "r_wed01", "quote_id": "q_wed_a", "buyer_id": "demo-catering", "created_at": "...", "summary": "Wedding for 120, 93.15 EUR incl. VAT, delivery 20 Nov", "reasons": ["SPLIT_DELIVERY", "SUBSTITUTION"], "expires_at": "..."}]
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
| `POST /gateway/quotes` | `request_quote` | `{request_id?, buyer_id, agent_id, requested_date, lines: [{item_id, product_id, quantity_sales_units}], budget?, option_label?}` | `Quote` with the policy decision |
| `POST /gateway/quotes/{quote_id}/request-approval` | `request_approval` | `{agent_id, explanation, alternative_quote_ids?}` | `{"approval_id", "status": "PENDING"}` |
| `POST /gateway/orders` | `place_order` | `{quote_id, agent_id, idempotency_key}` | `Order`. `403 NEEDS_APPROVAL` without a matching approval, `410 QUOTE_EXPIRED`, `422 BLOCKED`. Same key → same order |
| `GET /gateway/orders/{order_id}` | `get_order_status` | | `Order`, or the pending approval state |

## Openness

- `GET /openapi.json`: the backend's generated spec. The web links to it as "Open spec".
- `GET /health`: `{"status": "ok", "valio_api": "ok", "database": "ok"}`. Fixture: `health.json`.
