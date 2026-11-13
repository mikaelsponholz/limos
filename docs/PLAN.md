# Limos plan

Limos is a purchasing agent for professional kitchens, built at Junction 2026 for the Valio Aimo "Trusted Trade Agents" challenge, with side entries for the Gemini API, Codex and Aiven challenges.

A kitchen describes an event or picks a preset. Gemini expands it into an item list, searches the catalogue, checks price and availability and chooses products. The Limos gateway locks the price in a quote, deterministic rules decide whether it can be ordered alone or needs a human, and every step lands in an append-only decision log. Any outside agent (for example Claude Desktop over MCP) buys through the same gateway under the same guardrails.

Pitch: **the AI proposes, the rules decide, the human signs.** The value for the kitchen is time saved on routine search, comparison and ordering. Copy never makes speed promises or numbers we haven't measured.

This plan merges Mikael's and Johannes's versions (team decisions of 2026-11-13). The API contract is [`docs/api_contract.md`](api_contract.md).

## 1. Data findings (verified 2026-11-13)

Source files are fetched by `scripts/fetch_valio_data.py` into `data/valio/` and never committed: product data JSON (17 546 products, 85 MB), its schema, delivery reliability CSV (4 425 rows), carbon footprint CSV (11 033 rows), `openapi.json`.

### Valio mock API

- `GET /api/ping`, `POST /api/price`, `POST /api/availability`, and download links for the three data files. Auth: `X-API-Key`.
- **No quote or order endpoint.** Limos implements quotes and orders itself. Orders are simulated and say so.
- 1 to 1 000 product codes per call, undisclosed rate limits. We query only shortlists (normally under 20 products).
- **Price**: net and gross per sales unit, `comparisonPrice` per `KG` or `L`, VAT rate, list price, `netPriceFound`, `error`. Prices didn't change with the requested date. `validTo` is 9999-12-31. `catalogPrice / comparisonPrice` = kg or l per sales unit, used to cross-check pack sizes.
- **Availability** (40 products, 5 dates):
  - `requestedDate` didn't change the answer. Every deliverable product had `availableDate` 2026-11-16, so asking for an earlier date is a real late-delivery case.
  - `Stock` products report real amounts (7, 10, 22, 43, 319...) or "ample" sentinels (9 999 999 999). `Terminal` products always report 999 999 (unknown amount).
  - 6 of 40 had empty `scheduleLines`, 2 had amount 0. Unknown ids also return empty `scheduleLines`, not an error.
  - Terminal `closingTime` was 2026-11-12 (already past) while still offering 2026-11-16. A passed cut-off is a **warning flag, not a block**, or every Terminal product would fail.
  - Not checked: whether availability changes during the event. We re-check before ordering.

### Product data fields

| Need | Field | Notes |
|---|---|---|
| Name | `synkkaData.names[]` fi / en / sv | fi 100 %, en ~100 % |
| Origin | `countryOfOrigin` (ISO alpha-2, lowercase) | 99.9 %, `fi` on 7 849. Can disagree with the name |
| Finnish labels | `classifications[packagingMarkedLabel]`: `GOODS_FROM_FINLAND_BLUE_SWAN` 2 057, `MADE_IN_FINLAND_FLAG_WITH_KEY` 1 772, `FINNISH_HEART_SYMBOL` 826, `PUHTAASTI_KOTIMAINEN` 7 | |
| Organic | same group: `EU_ORGANIC_FARMING` 456, `KRAV_MARK`, `OFFICIAL_ECO_LABEL_SUN`, `SOIL_ASSOCIATION_ORGANIC_SYMBOL` | "luomu" in 473 names. Name-only evidence is weaker and shown as such |
| Allergens | `classifications[allergen]`: GS1 code + `CONTAINS`, `MAY_CONTAIN` or `UNDECLARED`. `classifications[nonAllergen]`: `FREE_FROM` | Fresh produce often has no allergen data. Missing data is never "safe" |
| Allergen codes | AM milk, ML lactose, AW gluten cereals, UW wheat, GB barley, NR rye, GO oats, ST spelt, AE egg, AF fish, AC crustaceans, UM molluscs, AP peanut, AN tree nuts (SA almond, SH hazelnut, SW walnut, SC cashew, SP pecan, SR brazil, SM macadamia...), AY soy, AS sesame, BC celery, BM mustard, AU sulphites, NL lupin | Mapping in `gateway/catalog/allergens.py` |
| Lactose, diet | `classifications[nutritionalClaim]`: `FREE_FROM_LACTOSE` 4 178, `LOW_LACTOSE` 321, `FREE_FROM_GLUTEN`, `VEGAN`, `VEGETARIAN` | `LOW_LACTOSE` is not lactose-free |
| Ingredients text | `synkkaData.keyIngredients[]` (80 %) | Allergens in capitals. Secondary check only |
| Units | `salesUnit` (KI 8 005, ST 5 607, KG 1 068...), `baseUnit`, `units[]` `sizeInBaseUnits`, `allowedLotSize` | Unit codes untranslated; we keep a label map |
| Pack size | `synkkaData.unitConversions[]` net weight / volume (82 %), else the name, else the price ratio | Unknown pack size → approval |
| Storage class | `packagingMarkedLabel` `warmTemperature` / `coolTemperature` / `freezeTemperature`, `temperatureCondition` code | For the storage add-on |
| Substitutions | `substitutions[]` | 189 products, 250 rules. Valio-approved |
| Category | `category` code | 349 codes, no names. Not used in rules |
| Reliability | CSV, 0 to 1 | Median 1.0, p10 0.94. Missing = unknown, not bad |
| Carbon | CSV, kg CO2e per kg (group average) | Median 2.1, p90 7.1 |

No purchase history exists in the data, so no rule depends on one.

## 2. Architecture

```
Kitchen buyer (preset, event form or free text)      External agent (Claude Desktop, script)
        |                                                     |  MCP / REST
        v                                                     |
[Limos web, Next.js]  (Johannes)                              |
        | REST: /requests, /approvals, /audit                 |
        v                                                     v
[Limos backend, FastAPI]  (Mikael)
   agent/      Gemini buyer agent: intent, event expansion, choice, explanation
       | function calling over the gateway's HTTP tools (same as any outside agent)
   gateway/    tools: search, offers, quotes, approvals, orders | policy | decision log
       |                     |
       v                     v
[Valio Aimo mock API]   [Store: JSON file → Aiven PostgreSQL]
```

- **Two parties.** The Gemini agent is a client of the gateway and calls it over HTTP, exactly like Claude Desktop or the plain script. The gateway trusts no client: it enforces mandates, price locks and approvals itself.
- **Code, not AI:** search over the local catalogue, unit conversion, price lock, quote expiry, content hash, idempotency, policy outcome, decision log.
- **AI (Gemini):** parse free text, expand an event into items, pick among candidates the code found, propose substitutes, write the explanation. The explanation may only use facts the code supplied (prices, dates, labels).
- **Never**: an LLM approves, writes the decision log, or sees more than a short candidate list.

### Stack

- Backend: Python 3.12, FastAPI, Pydantic v2, httpx, `google-genai`, `mcp` (official Python SDK), pytest, ruff, mypy, managed with `uv`. Storage: psycopg 3 for Aiven later.
- Web: Next.js (App Router), TypeScript, Tailwind, in `web/`. TS types are generated from the backend's OpenAPI with `openapi-typescript`.
- Gemini: `GEMINI_MODEL` (current Flash) for everything. `GEMINI_EXPANSION_MODEL` defaults to the same Flash model; Phase 3 tests Pro for event expansion and records the result.

### Repo layout

```
gateway/            FastAPI app
  api/              routes: buyer, approver, gateway tools, health
  schemas.py        Pydantic models = the API contract
  valio/            Valio client, models, cache, retry
  catalog/          slim index, search, labels, allergens, pack size
  offers.py         shortlist → comparable offers
  quotes.py         price lock, expiry, content hash
  orders.py         idempotent simulated orders
  policy/           rules, mandates, outcome
  store/            Store interface: memory, JSON file, Postgres
  audit.py          decision log with hash chain
agent/              Gemini buyer agent (intent, expansion, tool loop, explanation)
mcp_server/         MCP server over the gateway tools
clients/            plain script client (openness demo)
presets/            the four demo presets as JSON
scripts/            fetch_valio_data, build_catalog, ping_valio, list_models, export_openapi, find_demo_cases
contract/openapi.json   exported OpenAPI snapshot (committed, web generates types from it)
fixtures/api/       mock JSON for every contract shape
tests/
web/                Next.js UI (Johannes)
docs/               PLAN, api_contract, POLICY, ARCHITECTURE
data/valio/         Valio files (git-ignored, fetched by script)
.runtime/           local store and recordings (git-ignored)
```

Zones: Mikael owns everything outside `web/`. Johannes owns `web/`. `docs/api_contract.md`, `gateway/schemas.py` and `fixtures/api/` change only by agreement.

## 3. Request flow

1. **Request** (`POST /requests`): a preset id, an event form, an item list or free text. Returns `202` with a `request_id`. The run is a background task. The UI polls `GET /requests/{id}`.
2. **Intent** (Gemini Flash): free text becomes the structured intent. Fields the buyer set always win.
3. **Event expansion** (Gemini, structured output): each dish becomes items with a per-serving quantity. Code multiplies by servings, adds waste (mandate, default 5 %) and merges the same item across dishes. Per-guest sanity bounds add assumption notes, never silent fixes.
4. **Search** (gateway `search_products`, code): token search over fi / en names and key ingredients. Hard filters (allergen CONTAINS, dietary `violated`, sales block) run before Gemini sees anything.
5. **Choice** (Gemini over tools): picks the relevant candidates (whipping cream, not cream cheese) from the list. Ids are validated against the list.
6. **Offers** (gateway `check_offers`): one Valio price and one availability call for the shortlist, then unit conversion to sales units with `allowedLotSize`, `dietary_check`, flags, reliability and CO2.
7. **Ranking** (code): good / better / best per item (section 5). The default pick follows the buyer's priority.
8. **Exceptions** (code, plus Gemini for agent substitutes): section 6.
9. **Quote** (gateway `request_quote`): locks prices, sets `expires_at`, computes `content_hash`, runs the policy engine. Outcome `AUTO`, `NEEDS_APPROVAL` or `BLOCK`.
10. **Order or approval**: `AUTO` → the agent calls `place_order` with an idempotency key. `NEEDS_APPROVAL` → the agent calls `request_approval`; a human approves one option bound to its `content_hash`; the gateway re-checks availability and places the order. `BLOCK` → the request ends with the rule and reason.
11. **One order per event.** A request produces at most one order.

## 4. Guardrails ("the rules decide")

Pure Python in `gateway/policy/`, with no LLM calls. Mandates per buyer live in `gateway/policy/mandates.json` (readable config, placeholders for now). Every rule has a pytest test for each outcome it can produce. Every quote gets one outcome and the list of rules that fired, each with a plain description.

| Rule id | When | Outcome |
|---|---|---|
| `AGENT_NOT_PERMITTED` | Agent id not allowed by the buyer's mandate | BLOCK |
| `SALES_BLOCK` | Product has a sales block | BLOCK (agent picks another) |
| `NOT_DELIVERABLE` | Empty schedule or amount 0 | BLOCK (agent picks another) |
| `PRICE_MISSING` | Price `error` | BLOCK (agent picks another) |
| `ALLERGEN_CONTAINS` | Contains an excluded allergen | BLOCK |
| `DIETARY_VIOLATED` | Data contradicts a required constraint (lactose-free asked, product contains lactose) | BLOCK |
| `SPEND_HARD_CAP` | Total over the mandate's hard cap | BLOCK |
| `BUDGET_HARD_STOP` | Total over the event budget + 15 % | BLOCK |
| `ALLERGEN_MAY_CONTAIN` | May contain an excluded allergen | NEEDS_APPROVAL |
| `ALLERGEN_DATA_MISSING` | Allergens excluded and the product has no allergen data | NEEDS_APPROVAL |
| `DIETARY_UNKNOWN` | A required constraint can't be confirmed from data | NEEDS_APPROVAL |
| `PREFERENCE_REQUIRED_UNMET` | No candidate meets a `require` preference (for example kotimainen); best available offered | NEEDS_APPROVAL |
| `SPEND_SOFT_LIMIT` | Total over the mandate's auto limit | NEEDS_APPROVAL |
| `LINE_SOFT_LIMIT` | One line over the line limit | NEEDS_APPROVAL |
| `BUDGET_EXCEEDED` | Total over the event budget | NEEDS_APPROVAL |
| `LATE_DELIVERY` | `available_date` after `deliver_by` | NEEDS_APPROVAL |
| `PARTIAL_QUANTITY` | Quote covers less than needed | NEEDS_APPROVAL |
| `SPLIT_DELIVERY` | One item split across products | NEEDS_APPROVAL |
| `SUBSTITUTION` | Any substitute, Valio-approved or agent-proposed (shown separately) | NEEDS_APPROVAL |
| `OVERBUY` | Ordered more than 25 % above need after pack rounding | NEEDS_APPROVAL |
| `PACK_SIZE_UNKNOWN` | Can't convert need to sales units | NEEDS_APPROVAL |
| `TERMINAL_LOW_RELIABILITY` | Terminal product with reliability under 0.90 | NEEDS_APPROVAL |
| `PRICE_CHANGED` | A re-quote changed a locked price | NEEDS_APPROVAL |
| `CUTOFF_PASSED` | `closingTime` in the past | flag only (see findings) |

Placeholder mandate (per buyer, editable): auto limit €1 500 net, line limit €250, hard cap €5 000, max overbuy 25 %, waste 5 %, allowed agents.

**Quote rules (code):** `expires_at` = now + 30 min, never past a future `closingTime`. `content_hash` = SHA-256 over the canonical JSON of lines, totals, delivery date and expiry. An approval stores the hash it approved; `place_order` refuses a quote that is expired, changed, or `NEEDS_APPROVAL` without a matching approval. The same idempotency key returns the same order.

Dropped (no purchase history in the data): price versus last purchase, new product, category allow-lists.

## 5. Ranking: good, better, best

Only candidates that passed the hard filters are ranked. Score 0 to 100:

- Preference fit, 40: each `prefer` met. Label evidence beats name-only evidence.
- Price, 30: comparison price per kg or l against the cheapest candidate.
- Pack fit, 15: low waste after rounding.
- Delivery reliability, 10. Missing = neutral.
- CO2, 5: kg CO2e per kg against the other candidates. Missing = neutral.

**Good** = cheapest that meets every `require`. **Best** = highest preference fit. **Better** = highest total score of the rest. Priority `price` / `balanced` / `quality` picks the default. The quote shows `co2e_total_kg` where data exists. Offer reasons are composed from code facts.

## 6. Exceptions

| Exception | Agent action | Rule |
|---|---|---|
| Partial stock | Split across the next product, or a substitute, offered as options | `SPLIT_DELIVERY`, `PARTIAL_QUANTITY` |
| Late delivery | Candidate available in time; else show the earliest date | `LATE_DELIVERY` |
| Not deliverable, sales block, no price | Next candidate, then Valio's `substitutions[]`, then an agent-proposed substitute | `SUBSTITUTION` |
| Required preference unmet | Best available plus what is missing | `PREFERENCE_REQUIRED_UNMET` |
| Allergen risk | Prefer candidates with full data | `ALLERGEN_*` |
| Over budget | A cheaper option (lower tier per line, biggest savings first) | `BUDGET_EXCEEDED` |
| Quote expired before ordering | Re-quote; if any price moved, approval | `PRICE_CHANGED` |
| Valio errors, 429, timeouts | Retry with backoff; orders are safe to retry by idempotency key | step `ERROR` if exhausted |
| Gemini invalid output | One repair retry, then fail the step clearly | request `FAILED` |

Approval `options` are alternative quotes (at most 3): the recommended one and a safer or cheaper one.

## 7. Decision log

- Append-only `Step` entries per request: seq, time, actor (`user`, `agent`, `gateway`, `policy`, `human`), type, a plain title, detail, the model for LLM steps.
- Written only by the gateway and the agent runner, never by the LLM's text directly.
- Tamper evidence: each step stores `prev_hash` and `hash` (SHA-256). `GET /audit/{request_id}` returns the chain and whether it verifies.
- `Store` interface: memory (tests), JSON file in `.runtime/` (default), Aiven PostgreSQL (Phase 6). Chosen by `STORE=memory|file|postgres`.

## 8. Presets (demo scenarios)

| Preset | Guests | Must trigger |
|---|---|---|
| `restaurant_weekly` | item list | Every line AUTO, the agent orders alone, the log names the rule set that allowed it |
| `wedding_120` | 120 | Partial stock with a split option and a Valio-approved substitute → approval → order |
| `birthday_kids_25` | 25 | Nut-free; a may-contain-nuts product → `ALLERGEN_MAY_CONTAIN` approval with a safe option |
| `company_lunch_60` | 60 | Kotimainen required; budget set so the total exceeds it → `BUDGET_EXCEEDED` approval with a cheaper option |

Plus free text. Presets are built from data, not luck: `scripts/find_demo_cases.py` finds products that trigger each case (for example courgette 460025, not deliverable, with Valio substitute 460150 in stock). A test per preset runs against recorded responses and fails unless the expected outcome and rules appear. `uv run python scripts/demo_check.py` re-runs all presets live before the demo.

## 9. Openness and path to production

- The backend's OpenAPI at `/openapi.json`, snapshot committed in `contract/openapi.json`.
- **MCP server** (`mcp_server/`): the gateway tools `search_products`, `check_offers`, `request_quote`, `request_approval`, `place_order`, `get_order_status` for an external agent such as Claude Desktop (stdio entry point, plus Streamable HTTP). It calls the gateway's HTTP API, so the guardrails, approvals and decision log are exactly the same. There is no approve tool: approval stays human.
- A plain script client (`clients/plain_client.py`) places the same order through REST.
- Standards: OpenAPI 3.1, MCP, JSON Schema, GS1 allergen and label codes, ISO 3166, ISO 8601.
- To production (`docs/ARCHITECTURE.md`): customer-specific prices via a real customer id, a real order API behind `orders.py`, mandates per customer in the database, real auth for agents (OAuth client credentials) and approvers, Aiven PostgreSQL, scheduled re-checks before cut-off.

## 10. Phases

Gate for every backend phase: `uv run ruff check . ; uv run ruff format --check . ; uv run mypy gateway agent mcp_server ; uv run pytest -q` (each must pass). Each phase is about 1 to 2 hours. The web track (Johannes) runs in parallel against `fixtures/api/` and switches to the backend when Phase 1 is done.

### Phase 0: Backend scaffold and data

1. STANDARD: `pyproject.toml` (uv, Python 3.12, FastAPI, Pydantic v2, httpx, google-genai, pytest, ruff, mypy), FastAPI app with `GET /health`, the error format from the contract, CORS for the web origin, MIT `LICENSE`, `.env.example` (`VALIO_API_KEY`, `VALIO_BASE_URL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_EXPANSION_MODEL`, `STORE`, `DATABASE_URL`, `WEB_ORIGIN`), `.gitignore` covering `data/`, `.runtime/`, `.venv/`, `__pycache__/`.
2. STANDARD: `scripts/fetch_valio_data.py` downloads the three data files and `openapi.json` via the download endpoints (following redirects) into `data/valio/`. `gateway/valio/`: async client for ping, price and availability with Pydantic models matching Valio's `openapi.json`, timeout, retry with backoff on 429 and 5xx, batches of at most 1 000, a 5-minute cache, sentinel normalisation. `scripts/ping_valio.py` prints a compact table for three products. Tests with mocked httpx.
3. ENGINE: `scripts/build_catalog.py` writes `data/valio/catalog.slim.json`. `gateway/catalog/` loads it once: names, origin, Finnish label and organic evidence, allergens (contains, may contain, undeclared, free from) mapped to contract codes, claims, units, pack size resolver, lot size, storage class, substitutes, reliability, CO2, token search. Tests with invented records only.
4. STANDARD: `agent/llm.py` Gemini wrapper: model ids from env, structured output from Pydantic models with one repair retry, a function-calling loop, a fake LLM for tests. `scripts/list_models.py` lists the models for the key; pin the current Flash id as the default in `.env.example` and record it in Decisions.
5. TEST: `gateway/schemas.py` with Pydantic models for every shape in `docs/api_contract.md`. A test validates every file in `fixtures/api/`. `scripts/export_openapi.py` writes `contract/openapi.json`, and a test fails when the snapshot is stale.

### Phase 1: Thin end-to-end (target: Saturday midday)

1. ENGINE: `gateway/offers.py`: shortlist → `Offer` with unit conversion, lot size, `dietary_check`, flags, reliability, CO2.
2. ENGINE: `gateway/policy/` with the mandate file and the rules in section 4 except `PRICE_CHANGED`. One test per rule and outcome.
3. ENGINE: `gateway/quotes.py` and `gateway/orders.py`: price lock, expiry, content hash, approvals bound to the hash, idempotent simulated orders. `gateway/store/` memory and JSON file. `gateway/audit.py` with the hash chain.
4. STANDARD: routes from the contract: gateway tools, `POST/GET /requests`, `GET /presets`, approvals, audit. A deterministic agent runner for the `restaurant_weekly` preset (item list, top candidate by code, no LLM yet) that runs search → offers → quote → AUTO → order.
5. TEST: end-to-end test with mocked Valio for the AUTO path and for a NEEDS_APPROVAL → approve → order path, including a 409 on a changed hash and an idempotent retry. `scripts/smoke.py` runs it against the live API.

### Phase 2: MCP server

1. STANDARD: `mcp_server/` with the official `mcp` SDK. Tools from section 9, each calling the gateway's REST API with an agent id. Stdio entry point (`uv run limos-mcp`) and Streamable HTTP. No approve tool.
2. TEST: tests with an MCP client session against a running test gateway: the AUTO order works, a NEEDS_APPROVAL quote can't be ordered until a human approves through REST.
3. COPY: `docs/MCP.md` with the Claude Desktop config snippet and a short walkthrough. `clients/plain_client.py` for the REST openness demo.

### Phase 3: Gemini agent

1. ENGINE: intent parsing from free text (Flash, structured), buyer-set fields win.
2. ENGINE: event expansion (structured, per serving): scaling, waste and merging in code, sanity notes. `scripts/eval_expansion.py` runs the four presets with Flash and Pro and writes a short comparison. Record the chosen model in Decisions.
3. ENGINE: the tool loop: Gemini calls the gateway tools over HTTP to pick candidates and request a quote, and writes `agent_explanation` from the facts it was given. Ids and numbers it returns are validated against tool results.

### Phase 4: Exceptions and presets

1. ENGINE: partial stock with split options, late delivery, not deliverable, Valio substitutes then agent substitutes (Gemini proposes, rules decide), required preference unmet, budget exceeded with a cheaper option, quote expiry with re-quote and `PRICE_CHANGED`. Approval options as alternative quotes.
2. TEST: `scripts/find_demo_cases.py`, the four preset files in `presets/`, recorded responses for each, and one test per preset that asserts its outcome and fired rules.

### Phase 5: Ranking

1. ENGINE: good / better / best scoring with reliability and CO2 as secondary signals, priority-based default, `co2e_total_kg`, reasons composed from code facts.

### Phase 6: Aiven, docs and demo hardening

1. DB_RLS: Postgres `Store` for requests, quotes, approvals, orders and the decision log, plus a migration SQL file. Mikael applies it to Aiven. File store stays the fallback.
2. COPY: `README.md` (pitch, setup, fetch script, presets, MCP), `docs/ARCHITECTURE.md`, `docs/POLICY.md` (rule table and mandate fields).
3. STANDARD: record and replay of Valio and Gemini responses in `.runtime/` for an offline demo, `scripts/demo_check.py`, a clear message when a rate limit is hit.

### Phase 7 (add-on): Storage capacity rule

1. ENGINE: the mandate gets cold, frozen and dry storage capacity (litres or kg). The rule `STORAGE_CAPACITY` sums the order by storage class (temperature labels, volume or weight from `unitConversions`) and needs approval above capacity. Unknown size counts as unknown, not zero.

### Stretch

- Menu or order-list photo: Gemini multimodal reads it into the request (`multipart` on `POST /requests`).
- Gemma as a second, open-model client through MCP or REST.

## Rules for every phase

- `data/`, `.env*` and `.runtime/` are never committed, printed or copied. Valio data is fetched by script. Tests and fixtures use invented products only.
- Keys stay in the backend. The web never calls Valio or Gemini.
- Policy, price lock, orders and the decision log are deterministic code with tests. No LLM calls inside them.
- Only shortlists go to Valio and to Gemini, never the catalogue.
- The contract (`docs/api_contract.md`, `gateway/schemas.py`, `fixtures/api/`, `contract/openapi.json`) changes together and only by agreement with Johannes. Bump the version.
- All code is written during the event and licensed MIT.

## Decisions

- 2026-11-13: Scope: an event (or preset, item list, free text) is expanded into an item list. Four presets: wedding, children's birthday, company lunch, restaurant weekly order.
- 2026-11-13: Stack: Python FastAPI backend and agent (Mikael), Next.js UI in `web/` (Johannes). TS types generated with `openapi-typescript`.
- 2026-11-13: One shared contract, `docs/api_contract.md` v0.2, based on Johannes's v0.1 with additions.
- 2026-11-13: Quote and price lock, with approval bound to the content hash, are in from Phase 1.
- 2026-11-13: One order per event.
- 2026-11-13: Rules needing purchase history are dropped. Limits are placeholders in the mandate file.
- 2026-11-13: Substitutions always need approval, also Valio-approved ones. Reason: the buyer asked for something else, and the UI labels the source.
- 2026-11-13: Passed `closingTime` is a flag, not a block (Terminal data shows past cut-offs with valid delivery dates).
- 2026-11-13: Gemini Flash for everything; Pro is tested for event expansion in Phase 3.
- 2026-11-14: `GEMINI_MODEL` pinned to `gemini-3.8-flash`, the current Flash on our key (`scripts/list_models.py`). The key has no `gemini-3.1-pro`; Phase 3 chooses the Pro-class model to test for dish expansion from the key's list (`gemini-2.5-pro` or a `gemini-4-*` model) and records it here. `GEMINI_EXPANSION_MODEL` stays empty (Flash) until then.
- 2026-11-14: pytest uses `--basetemp=.pytest-tmp` (in `pyproject.toml`) because a sandbox-created temp folder under `%TEMP%` can be unreadable.
- 2026-11-13: MCP server is its own phase right after the thin end-to-end flow.
- 2026-11-13: Storage capacity rule is an add-on phase. Menu photo is stretch.
- 2026-11-13: Pitch is time saving, without speed promises.
- 2026-11-13: Orders are simulated because the Valio mock API has no order endpoint.

## Open questions

- Organisers: is a past `closingTime` for Terminal products intended?
- Organisers: does availability change during the event?
