> **Superseded.** This was the first draft. The current plan is `docs/PLAN.md` and the contract is `docs/api_contract.md` v0.2. Kept for history only.

# PROMPT 0: Limos master context

You are the builder for **Limos**, our team's Junction 2026 hackathon project (Helsinki, 13 to 15 Nov 2026). Read this whole document before writing any code. It is the single source of truth for what we build and why. Later prompts will each ask for one piece; always keep this context in mind.

Do not start building yet. Your task for this prompt is at the end.

---

## 1. What we are building

Valio Aimo is a Finnish foodservice wholesaler: 20,000+ professional kitchens, 25,000 products, 700 suppliers. Their challenge, "Trusted Trade Agents", asks:

> Build the prototype that lets an AI purchasing agent buy food from a wholesaler: safely, openly, and with the right human approval.

The flow that must run end to end: **Request → Lookup → Order (at a fixed price) → Approval** (the agent decides alone, or a human signs off).

Example request: a hotel chain's agent asks *"I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our requirements based on price and availability."*

**Limos** is the trusted gateway between any outside buying agent and Valio Aimo, plus a reference buyer agent built on Gemini.

Our one-line pitch: **"The AI proposes, the rules decide, the human signs."**

### Judging (Valio Aimo)
| Criterion | Weight |
|---|---|
| Working end-to-end demo | 30% |
| Solving the user's need | 20% |
| Autonomy and controls (guardrails) | 20% |
| Robustness to exceptions | 15% |
| Openness and path to production | 15% |

They explicitly want: open standards (not a vendor black box), **not a chatbot**, and **query the APIs only when needed, never for all products**. Not a production system, but a full flow that works with a clear human sign-off model and well-documented choices.

### We also submit the same project to two side challenges
- **Gemini API (Google DeepMind):** Gemini must do meaningful, visible work. It is our buyer agent: parsing the request (text, and later a photo of a handwritten order list), calling tools via function calling, choosing between candidates and writing the explanation.
- **Aiven:** the database is Aiven PostgreSQL (quotes, orders, approvals, decision log, product index). Optional later: Aiven Valkey as a cache for price and availability lookups.

---

## 2. Architecture

```
Kitchen user (text, or photo of an order list)
        |
        v
[Gemini buyer agent]          AI: understands, searches, chooses, explains
        |  function calling over HTTP
        v
[Limos gateway  (FastAPI)]    CODE: search, quote, order, policy, approvals, audit log
        |                \
        v                 v
[Valio Aimo mock APIs]   [Aiven PostgreSQL]
 price, availability      product index, quotes, orders, approvals, decision log
        ^
[Approval UI]  human sees pending approvals and the decision log, approves or rejects
```

### Core principle: what is AI and what is code
| Part | AI or code |
|---|---|
| Understanding the request, unit intent, constraints | AI (Gemini) |
| Choosing between candidates, proposing substitutes | AI (Gemini) |
| Writing the human-readable "why" | AI (Gemini) |
| Product search over the static catalogue | Code |
| Price lock, quote expiry, order placement, idempotency | Code |
| Policy decision: AUTO / NEEDS_APPROVAL / BLOCK | Code (deterministic, readable rules) |
| Decision log | Code (never written by the AI) |

The AI must never be able to approve its own order. Approval rules are plain code: same input, same output, every time.

### Single agent, two parties
Do not build a multi-agent system. One Gemini agent on the buyer side, one gateway on the seller side. They are separate systems that do not trust each other. Openness is shown by letting a second client (a plain script, later an open model such as Gemma) place the same order through the same gateway.

---

## 3. Valio Aimo materials (already downloaded)

All files are in `data/valio/`:

| File | What it is |
|---|---|
| `valio_aimo_junction26_product_data.json` | 17,546 products, 87 MB. **Never send this to an LLM and never commit it.** |
| `valio_aimo_junction26_product_data_schema.json` | JSON Schema of the product file. Read it. |
| `valio_aimo_junction26_delivery_reliability.csv` | `productId,deliveryReliability` (0 to 1), about 4,400 products |
| `valio_aimo_junction26_carbon_footprint.csv` | `productId,AVG kg CO2-eq/kg` (product group average), about 11,000 products |
| `openapi.json` | OpenAPI spec of the mock APIs. Read it. |

### Mock API facts (verified)
- Base URL: `https://junction26.valioaimo.fi/api`
- Auth: header `X-API-Key`, value from env var `VALIO_API_KEY`. Never hardcode or log it.
- `GET /ping`
- `POST /availability` and `POST /price`, both with body `{"customer": "...", "requestedDate": "YYYY-MM-DD", "products": ["460000", ...]}`. 1 to 1,000 product codes per call. Customer number does not change the result in this challenge.
- **Undisclosed rate limits.** Only call the APIs for a narrowed-down shortlist (typically under 20 products). Never loop over the catalogue.
- **There is no quote API and no order API.** Limos implements quotes and orders itself. This is our core contribution.

### Important fields
Availability, per product:
- `closingTime`: order cut-off. If now is past it, the goods will not arrive on `availableDate`.
- `salesBlock`: if true, cannot be ordered.
- `logisticModel`: `Stock` (Valio knows the real quantity) or `Terminal` (delivered from the supplier; quantity unknown, API returns 999999).
- `scheduleLines`: empty means not deliverable on any date. Otherwise one line with `availableAmount` (in `salesUnit`), `availableDate` (may differ from the requested date!) and `salesUnit`.

Price, per product:
- `catalogPrice.value` (excl. VAT) and `valueWithTax`, per sales unit, with `validFrom`/`validTo`.
- `listPrice`: price before customer discount. `netPriceFound`: true when a customer-specific price applies.
- `comparisonPrice` per `comparisonUnit` (`L` or `KG`): use this to compare products of different pack sizes.
- `tax.taxRate` (e.g. 13.5 for food).
- `error`: true when no price exists.

Product data:
- `id` is the product code used everywhere.
- `salesUnit` (e.g. `KI` = case, `ST` = piece), `baseUnit`, `units[]` (`unitId`, `sizeInBaseUnits`) for pack conversions, `allowedLotSize` (order quantity must be a multiple of it).
- `synkkaData.names[]` in fi/sv/en, `classifications[]` with groups such as `allergen` (unit `CONTAINS` or `FREE_FROM`), `nutritionalClaim`, `nutritionalContent`, `unitConversions` (volume/weight per packaging level).
- `substitutions[]` on 196 products: **Valio's own approved substitutes.** Prefer these over AI-invented substitutes.
- Data quality varies (e.g. fresh produce often lacks allergen data). Missing data must never be treated as "safe".

### Verified example for the demo
Lactose-free milk exists in many forms, e.g. a 1 l carton sold per `ST` with Valio-approved substitutes, the same milk sold per `KI` case from a Terminal supplier, and a 10 l pack sold per `KI` of 2. A price call can return a catalogue price below the list price when a customer net price applies.

---

## 4. Gateway contract (high level; details come in later prompts)

Tools exposed by the gateway over REST with an OpenAPI spec (and later as MCP tools):

| Tool | Purpose |
|---|---|
| `search_products(query, attributes, category, limit)` | Search the local product index (Postgres), never the Valio API. Returns a short candidate list. |
| `check_offers(product_ids, quantity, unit, requested_date)` | For a shortlist: calls Valio price + availability, merges delivery reliability and carbon, converts units, returns comparable offers. |
| `request_quote(lines, requested_date, buyer_id, agent_id)` | Locks prices into a quote with a quote ID, expiry (short, e.g. 30 min, and never past the price `validTo` or the `closingTime`) and a content hash. Runs the policy engine. |
| `place_order(quote_id, idempotency_key)` | Only accepts an unexpired quote. If policy said NEEDS_APPROVAL, requires a matching approval. Same idempotency key never creates a second order. |
| `get_order_status(order_id)` | Status incl. pending approval. |

Human-only endpoints (not exposed to the agent): `GET /approvals`, `POST /approvals/{id}/approve`, `POST /approvals/{id}/reject`, `GET /audit/{quote_or_order_id}`.

### Policy engine (deterministic code)
Every quote gets exactly one outcome, with the rule that caused it:
- **AUTO:** exact match to the request, under the buyer's spend limit, approved category, price within X% of the last purchase, delivery on time, all dietary constraints confirmed by data.
- **NEEDS_APPROVAL:** substitution, split delivery, partial quantity, later date than requested, over the soft spend threshold, new product, price jump, Terminal product with low delivery reliability, missing allergen data where the request has a dietary constraint.
- **BLOCK:** forbidden category, over the hard cap, agent outside its permissions, a substitute that **breaks a dietary/allergen constraint** (e.g. lactose-free requested, substitute contains lactose), product has `salesBlock`.

An approval is bound to the exact quote hash. If anything in the quote changes after approval, the approval no longer matches.

Buyer mandates (spend limits, allowed categories, thresholds) are stored per buyer organisation in the database and are readable configuration, not code buried in prompts.

### Decision log
For every request: what was asked, what was searched, which candidates were considered, which was chosen and why (Gemini's text), which policy rule fired, who approved, and timestamps. Written by the gateway, append-only.

---

## 5. Exceptions we must handle (15% of the score)
- Partial availability or a later `availableDate` than requested.
- Substitution needed (prefer Valio's `substitutions[]`, then AI proposals; both go to a human).
- Unit conversion: litres to sales units, respecting `allowedLotSize` and pack sizes.
- Cut-off passed (`closingTime`).
- Quote expired or price changed before ordering: re-quote; if the price moved, human approval.
- `salesBlock`, empty `scheduleLines`, or price `error`.
- Valio API errors, timeouts or rate limiting: retry with backoff; orders are safe to retry via the idempotency key.

---

## 6. Demo (3 minutes, three scenarios)
1. **Happy path:** small routine order, the agent completes it alone, the log shows which rule allowed it.
2. **Over the limit:** the 1,000 L milk order goes over the spend threshold, the manager approves it on their phone, the order goes through.
3. **Exception:** only partial stock or a late date; the agent proposes a split plus a Valio-approved substitute, explains why, the human picks one.

Plus one openness moment: the same order placed by a plain script (and later Gemma) through the same gateway.

---

## 7. Stack and conventions
- **Python 3.11+**, **FastAPI**, **Pydantic v2**, **httpx**, **SQLAlchemy 2 or psycopg 3**, **pytest**.
- **Gemini:** `google-genai` SDK, model from env var `GEMINI_MODEL` (use the current Gemini Flash model by default), API key from `GEMINI_API_KEY`.
- **Database:** Aiven PostgreSQL via `DATABASE_URL`. A local Postgres or SQLite fallback is fine for tests.
- **Approval UI:** built separately (Lovable) against the human-only endpoints. Enable CORS for it.
- Config only via environment variables, loaded from `.env`. Provide `.env.example` with names and no values.

### Repo layout (target)
```
limos/
  data/valio/            Valio files (large JSON is gitignored)
  prompts/               these prompts
  gateway/               FastAPI app: valio_client, catalog, offers, quotes, orders, policy, audit
  agent/                 Gemini buyer agent
  clients/               second client script (openness demo)
  scripts/               one-off loaders (product index into Postgres)
  tests/
  docs/                  spec, policy model, path to production
  .env.example
  README.md
```

### Hard rules
1. Never commit `.env`, API keys or `data/valio/*.json`. Set up `.gitignore` first.
2. Never send the full product file or large parts of it to Gemini. Gemini only sees short candidate lists.
3. Never call Valio price/availability for more than a shortlist.
4. Policy, price locking and the decision log are plain deterministic code with tests. No LLM calls inside them.
5. Every policy rule has a pytest test (AUTO, NEEDS_APPROVAL and BLOCK cases).
6. Keep it simple and demo-first. A working happy path beats a half-built feature. Checkpoint: happy path end to end by Saturday midday.
7. When unsure about a product, a field or a Valio behaviour, check the data or the API instead of guessing, and say what you checked.
8. Write short, clear docstrings. The repo is part of the "openness and path to production" score.

---

## Your task for this prompt

1. Confirm you have read this context and summarise the plan back in max 10 bullet points.
2. Initialise the repo skeleton in the current folder `limos/` exactly as in the layout above: folders, empty `__init__.py` files, `.gitignore` (must cover `.env`, `data/valio/*.json`, `__pycache__`, `.venv`), `.env.example` with `VALIO_API_KEY`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `DATABASE_URL`, a `pyproject.toml` or `requirements.txt` with the stack above, and a short `README.md` (what Limos is, how to run, the one-line pitch).
3. Write `gateway/valio_client.py`: an async httpx client for `/ping`, `/price` and `/availability` with the API key from env, a timeout, retry with backoff on 429/5xx, and typed Pydantic response models matching `openapi.json`.
4. Add `scripts/ping_valio.py` that pings the API and fetches price + availability for three product codes for next Monday, and prints a compact table.
5. Do not build anything else yet.

**Report back:** the file tree, the output of `scripts/ping_valio.py`, and any surprises in the data or API.
