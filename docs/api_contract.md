# Limos API contract (draft v0.1)

Contract between the **web** (frontend) and the **backend** (gateway + Gemini agent). Both sides build against this. Change it only by agreement, and bump the version.

- Base URL: env `NEXT_PUBLIC_API_BASE` on the web side (e.g. `http://localhost:8000`).
- All bodies are JSON, UTF-8. Times are ISO 8601 with timezone. Money is EUR, numbers excl. VAT unless the field name says `with_tax`.
- CORS must allow the web origin.
- Errors: HTTP 4xx/5xx with `{"error": {"code": "string", "message": "human readable"}}`.

There are two kinds of endpoints:
- **Buyer side** (the kitchen user starts a request and follows it).
- **Human approver side** (the manager approves or rejects). These are never callable by the agent.

---

## Shared objects

### `Offer` (one candidate product)
```json
{
  "product_id": "464306",
  "name": "Valio Arki Eila kevytmaitojuoma 1 l laktoositon",
  "vendor": "VALIO OY",
  "sales_unit": "ST",
  "pack_description": "1 l carton, case of 20",
  "quantity_sales_units": 1000,
  "quantity_requested": {"value": 1000, "unit": "L"},
  "unit_price": 1.01,
  "list_price": 1.44,
  "comparison_price": {"value": 1.01, "unit": "L"},
  "line_total": 1010.00,
  "line_total_with_tax": 1146.35,
  "tax_rate": 13.5,
  "available_amount": 9999999999,
  "available_date": "2026-11-16",
  "requested_date": "2026-11-16",
  "closing_time": "2026-11-15T17:00:00+02:00",
  "logistic_model": "Stock",
  "delivery_reliability": 0.997,
  "carbon_kg_co2e_per_kg": 1.215,
  "is_substitute": false,
  "substitute_for": null,
  "substitute_source": null,
  "dietary_check": {"lactose_free": "confirmed"},
  "flags": []
}
```
- `substitute_source`: `"valio"` (from Valio's approved substitutions) or `"agent"` (proposed by AI) or `null`.
- `dietary_check` values: `"confirmed"`, `"violated"`, `"unknown"` (data missing).
- `flags`: list of short codes, e.g. `"LATE_DELIVERY"`, `"PARTIAL"`, `"TERMINAL_LOW_RELIABILITY"`, `"PRICE_JUMP"`, `"CUTOFF_PASSED"`, `"SALES_BLOCK"`, `"NEW_PRODUCT"`.

### `PolicyDecision`
```json
{
  "outcome": "NEEDS_APPROVAL",
  "rules_fired": [
    {"rule_id": "SPEND_SOFT_LIMIT", "description": "Order total 1,146.35 EUR exceeds the auto-approve limit of 500 EUR", "outcome": "NEEDS_APPROVAL"}
  ],
  "mandate": {"buyer_id": "hotel-helsinki-01", "auto_limit": 500, "hard_cap": 5000}
}
```
`outcome` is one of `AUTO`, `NEEDS_APPROVAL`, `BLOCK`.

### `Quote`
```json
{
  "quote_id": "q_7f3a",
  "status": "PENDING_APPROVAL",
  "lines": [ /* Offer */ ],
  "total": 1010.00,
  "total_with_tax": 1146.35,
  "created_at": "2026-11-14T10:02:00+02:00",
  "expires_at": "2026-11-14T10:32:00+02:00",
  "content_hash": "sha256:ab12...",
  "policy": { /* PolicyDecision */ },
  "agent_explanation": "Chose Valio Arki Eila 1 l: lowest price per litre among lactose-free options, in stock for Monday, 99.7% delivery reliability."
}
```
Quote `status`: `DRAFT`, `AUTO_APPROVED`, `PENDING_APPROVAL`, `APPROVED`, `REJECTED`, `BLOCKED`, `EXPIRED`, `ORDERED`.

### `Step` (one entry in the agent timeline / decision log)
```json
{
  "seq": 3,
  "at": "2026-11-14T10:01:12+02:00",
  "actor": "agent",
  "type": "TOOL_CALL",
  "title": "Checked price and availability for 4 products",
  "detail": {"tool": "check_offers", "product_ids": ["464306", "465039", "463038", "464398"]}
}
```
- `actor`: `user`, `agent`, `gateway`, `policy`, `human`.
- `type`: `REQUEST_RECEIVED`, `INTENT_PARSED`, `TOOL_CALL`, `CANDIDATES`, `CHOICE`, `QUOTE_CREATED`, `POLICY_DECISION`, `APPROVAL_REQUESTED`, `APPROVED`, `REJECTED`, `ORDER_PLACED`, `ERROR`.

---

## Buyer side

### `POST /requests`
Start a purchase request. Either text, an image, or both.
- JSON: `{"buyer_id": "hotel-helsinki-01", "text": "1000 L lactose-free milk by Monday", "requested_date": "2026-11-16"}`
- or `multipart/form-data` with fields `buyer_id`, `text` (optional), `image` (optional, jpg/png).

Response `202`:
```json
{"request_id": "r_91c2", "status": "RUNNING"}
```

### `GET /requests/{request_id}`
Poll every 1 to 2 s while `status` is `RUNNING`.
```json
{
  "request_id": "r_91c2",
  "buyer_id": "hotel-helsinki-01",
  "status": "WAITING_APPROVAL",
  "input": {"text": "1000 L lactose-free milk by Monday", "image_url": null},
  "intent": {"items": [{"description": "lactose-free milk", "quantity": 1000, "unit": "L", "constraints": ["lactose_free"]}], "deliver_by": "2026-11-16"},
  "steps": [ /* Step */ ],
  "candidates": [ /* Offer */ ],
  "quote": { /* Quote or null */ },
  "order": {"order_id": "o_55d1", "status": "CONFIRMED", "placed_at": "..."}
}
```
Request `status`: `RUNNING`, `WAITING_APPROVAL`, `ORDERED`, `BLOCKED`, `REJECTED`, `FAILED`.

### `GET /requests?buyer_id=...`
List of recent requests (id, created_at, short text, status, total).

---

## Human approver side

### `GET /approvals?status=PENDING`
```json
[{"approval_id": "a_12", "request_id": "r_91c2", "quote_id": "q_7f3a", "buyer_id": "hotel-helsinki-01", "created_at": "...", "summary": "1000 L lactose-free milk, 1,146.35 EUR incl. VAT", "reasons": ["SPEND_SOFT_LIMIT"], "expires_at": "..."}]
```

### `GET /approvals/{approval_id}`
Full detail: `{approval_id, status, quote: Quote, request: {...}, options: [Quote, ...]}`.
`options` is used in the exception scenario when the agent offers alternatives (e.g. split delivery vs. Valio substitute). Empty list otherwise.

### `POST /approvals/{approval_id}/approve`
Body: `{"approver": "Maija, Kitchen Manager", "quote_id": "q_7f3a", "content_hash": "sha256:ab12...", "comment": "OK"}`
- `quote_id` selects which option is approved (needed when there are several).
- The backend rejects with `409` if `content_hash` does not match the current quote (quote changed after it was shown).
Response: `{"status": "APPROVED", "order": {"order_id": "o_55d1", "status": "CONFIRMED"}}`

### `POST /approvals/{approval_id}/reject`
Body: `{"approver": "...", "reason": "Too expensive"}` → `{"status": "REJECTED"}`

### `GET /audit/{request_id}`
Full append-only decision log: `{"request_id": "...", "steps": [Step, ...]}`.

---

## Openness

### `GET /openapi.json`
The backend's generated OpenAPI spec. The web can link to it as "Open spec".

### `GET /health`
`{"status": "ok", "valio_api": "ok", "database": "ok"}`
