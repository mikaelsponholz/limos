# Limos plan

Limos is a purchasing agent for professional kitchens, built at Junction 2026 for the Valio Aimo "Trusted Trade Agents" challenge. A buyer describes an event and the menu. The agent expands dishes into ingredients, finds products, checks price and availability, handles exceptions, and either orders within its authority or asks a human to approve. Every decision lands in a human-readable audit trail.

Principle: **the AI proposes, the rules decide.** Gemini plans and suggests. Deterministic code decides what can be ordered.

## 1. Data findings (Step 1, 2026-11-13)

Source files live in `data/` (git-ignored, never redistributed): `openapi.json`, product data JSON (17 546 products, 85 MB), its schema, delivery reliability CSV (4 425 rows), carbon footprint CSV (11 033 rows).

### Valio mock API

- Endpoints: `GET /api/ping`, `POST /api/price`, `POST /api/availability`, plus download links for the three data files. Auth: `X-API-Key` header.
- **There is no order endpoint.** Ordering is simulated by our own order service behind an interface, so a real order API can replace it.
- Requests take `customer` (ignored), `requestedDate`, and 1 to 1 000 product codes. Undisclosed rate limits; the spec says to query only shortlisted products.
- **Price**: net and gross per sales unit, `comparisonPrice` per `KG` or `L`, VAT rate, list price, `netPriceFound` (2 of 40 sampled), `error`. Prices were identical for different requested dates. `catalogPrice / comparisonPrice` gives kg or litres per sales unit (461273: 107.97 / 5.40 = 20 L). We use this as a pack-size cross-check.
- **Availability** (40 products, 5 requested dates):
  - The response did not change with `requestedDate`. Every available product had `availableDate` 2026-11-16 (Monday), even when we asked for 2026-11-14. Requesting a date before that is a real late-delivery case.
  - `logisticModel` is `Stock` or `Terminal`. Terminal always reports 999 999 (unknown amount). Stock reports either real numbers (7, 10, 22, 43, 319, 552, 1 976 sales units, so partial stock is real) or sentinels such as 9 999 999 999 and 4 999 999 999 (ample).
  - 6 of 40 had empty `scheduleLines` (cannot be delivered), and 2 had amount 0 without a date. Unknown product ids also return an empty `scheduleLines`, not an error.
  - `closingTime` for Terminal products was already in the past (2026-11-12) while still offering 2026-11-16. We log it as a warning, not a block. Open question for the organisers.
  - Not checked: whether availability changes over time during the event. We re-check availability before placing an order and log any difference.

### Product data fields

| Need | Field | Coverage and notes |
|---|---|---|
| Name | `synkkaData.names[]` (fi, en, sv) | fi 100 %, en ~100 %, sv 92 % |
| Origin | `countryOfOrigin` (ISO alpha-2, lowercase) | 99.9 %. `fi` on 7 849 products. Can disagree with the name (a courgette named NL is coded `es`) |
| Origin, detail | `synkkaData.countriesOfOrigin[]` (numeric ISO, 246 = FI), `ingredients[].origins[]` (FARMING, MILKING, REARING...) | 82 % / 21 % |
| Finnish labels | `classifications[name=packagingMarkedLabel]`: `GOODS_FROM_FINLAND_BLUE_SWAN` 2 057, `MADE_IN_FINLAND_FLAG_WITH_KEY` 1 772, `FINNISH_HEART_SYMBOL` 826, `PUHTAASTI_KOTIMAINEN` 7 | |
| Organic | same group: `EU_ORGANIC_FARMING` 456, `KRAV_MARK` 26, `OFFICIAL_ECO_LABEL_SUN` 17, `SOIL_ASSOCIATION_ORGANIC_SYMBOL` 11 | 473 names contain "luomu". Fresh produce often has no labels at all, so we also read the name, marked as weaker evidence |
| Other labels | `SVANEN`, `EU_ECO_LABEL`, `RAINFOREST_ALLIANCE`, `FAIR_TRADE_MARK`, `MARINE_STEWARDSHIP_COUNCIL_LABEL`, `AQUACULTURE_STEWARDSHIP_COUNCIL` | |
| Allergens | `classifications[name=allergen]` values: GS1 code + `CONTAINS` / `MAY_CONTAIN`. `nonAllergen` holds `FREE_FROM` | Group present on 99.9 %, but often empty. Fresh produce usually has no allergen data |
| Allergen codes (GS1) | AM milk, ML lactose, AW cereals with gluten, UW wheat, GB barley, NR rye, GO oats, ST spelt, AE egg, AF fish, AC crustaceans, UM molluscs, AP peanut, AN tree nuts (SA almond, SH hazelnut, SW walnut, SC cashew, SP pecan, SR brazil, SM macadamia, ...), AY soy, AS sesame, BC celery, BM mustard, AU sulphites, NL lupin | Mapping lives in `lib/catalog/allergens.ts` |
| Lactose and diet | `classifications[name=nutritionalClaim]`: `FREE_FROM_LACTOSE` 4 178, `LOW_LACTOSE` 321, `FREE_FROM_GLUTEN` 3 047, `VEGAN` 2 959, `VEGETARIAN` 2 358, `FREE_FROM_EGGS` 418 | `LOW_LACTOSE` does not count as lactose-free |
| Ingredients text | `synkkaData.keyIngredients[]` (80 %), allergens in CAPITALS | Used for a secondary allergen check |
| Units | `salesUnit` (KI 8 005, ST 5 607, KG 1 068, PAK, BAG, RAS, PRK, BOT...), `baseUnit`, `units[]` with `sizeInBaseUnits` | Unit codes are untranslated technical codes; we keep our own label map |
| Pack size | `synkkaData.unitConversions[]` net weight / volume per packaging level (82 %), else parse the name ("3,3 dl", "10 x", "5200g"), else the price ratio above | Unknown pack size means the line needs review |
| Order step | `allowedLotSize` | 0.01 on 96 %, else 1 to 10 |
| Variable weight | `synkkaData.variableUnit` | true on 632 |
| Substitutions | `substitutions[]` | Only 189 products (1.1 %) |
| Category | `category` code, 349 distinct, names not included | Used for grouping only |
| Delivery reliability | CSV `productId,deliveryReliability` 0 to 1 | 4 425 products, median 1.0, p10 0.94. Missing means unknown, not bad |
| Carbon footprint | CSV `productId,AVG kg CO2-eq/kg` | 11 033 products, median 2.1, p90 7.1 |

## 2. Architecture

One Next.js (App Router) + TypeScript app.

```
app/                  UI pages and components (Johannes + designer)
app/api/              HTTP routes (Mikael)
lib/                  agent and backend (Mikael)
  catalog/            slim catalog loader, search, labels, allergens, pack size
  valio/              Valio API client: price, availability, cache, retry
  llm/                Gemini wrapper, model config, structured output, fake LLM
  agent/              run orchestrator, recipe expansion, matching, exceptions
  rules/              guardrails: policy, line and cart evaluation
  ranking/            good / better / best scoring
  orders/             order service interface + simulated implementation
  store/              RunStore and AuditStore interfaces + memory / JSON file / Postgres
  contract/           zod schemas mirroring types/contract.ts
types/contract.ts     JSON shapes the UI consumes (shared)
fixtures/contract/    mock JSON for each shape
scripts/              build-catalog, probe-valio, list-models
docs/                 PLAN, CONTRACT, ARCHITECTURE, POLICY, API
```

Libraries (each with a reason): `next`, `react`, `typescript`; `@google/genai` (Gemini, function calling, structured output); `zod` (runtime validation of LLM output and API bodies, one source for JSON Schema and OpenAPI); `vitest` (fast unit tests); `@modelcontextprotocol/sdk` (Phase 5). `pg` only in the Aiven stretch phase. No UI library decisions here: that is Johannes's zone.

### Run lifecycle

`POST /api/runs` returns a `runId` at once. The pipeline runs in the background and writes audit entries as it goes. The UI polls `GET /api/runs/:id` (about 1 s) and shows the trail filling in. Status: `planning`, `sourcing`, `awaiting_approval`, `ordering`, `ordered`, `failed`.

1. **Request**: structured form fields plus free text. Gemini Flash fills only blank fields from the text. Fields the buyer gave always win.
2. **Recipe expansion** (Gemini Pro, structured JSON): each dish becomes ingredients with a **per-serving** quantity, a Finnish and an English search term, and a category hint. Code multiplies by guests, adds a waste factor (policy, default 5 %), and merges the same ingredient across dishes (butter in cake and sandwiches becomes one cart line). Sanity bounds per guest raise an assumption warning, never a silent fix.
3. **Retrieval** (code): token search over fi and en names and key ingredients in the slim catalog. Hard filters (excluded allergens, `require` preferences, sales block) apply before the model sees anything.
4. **Relevance check** (Gemini Flash): from the top 20 candidates, which ones actually are the ingredient ("kuohukerma", not "kermajuusto"). The model may only return ids from the list (validated).
5. **Price and availability** (Valio API): one batched call each for the shortlist (at most about 5 per ingredient), for the delivery date.
6. **Quantities** (code): needed amount → pack size → sales units, rounded up to `allowedLotSize`. Overbuy above policy (default 25 %) is flagged.
7. **Ranking** (code): good / better / best, see section 4.
8. **Exceptions** (code first, Gemini Flash with function calling only for finding substitutes), see section 5.
9. **Rules** (code): every line and the whole cart get a decision with rule ids and a plain reason.
10. **Order or approval**: if nothing needs approval, the agent places the order itself. Otherwise approval items are created. When all are resolved, the rules run again, availability is re-checked, and the order is placed.

### Gemini use

- Models come from env: `GEMINI_PRO_MODEL` (planning, recipe expansion) and `GEMINI_FLASH_MODEL` (parsing, relevance, substitute search). Phase 0 lists the available models with the key and pins current ids in `.env.example`.
- Structured output: a zod schema → JSON Schema → `responseSchema`. Every response is validated, with one repair retry, then the run fails with a clear audit entry.
- Function calling: the substitute finder gets the tools `search_products`, `get_availability`, `get_prices` and `propose_line`. **No tool can place an order.** `propose_line` hands a candidate to the rules engine, which can reject it.
- Each LLM call is logged in the audit trail with the model id, purpose, a prompt hash and the parsed result.
- A fake LLM (fixture-backed) lets tests and offline demos run without the API.

## 3. Guardrails ("the AI proposes, the rules decide")

Pure TypeScript in `lib/rules`, configured by `lib/rules/policy.default.json`, documented in `docs/POLICY.md`, unit-tested per rule. Each rule has an id, returns `pass`, `needs_approval` or `block`, and writes a plain reason.

| Id | Rule | Default outcome |
|---|---|---|
| A1 | Product CONTAINS an excluded allergen | block (never orderable) |
| A2 | Product MAY_CONTAIN an excluded allergen | needs approval (policy: allow / approve / block) |
| A3 | Buyer excludes allergens, product has no allergen data | needs approval |
| A4 | Key ingredients text mentions an excluded allergen the codes don't (secondary check; Gemma in stretch) | needs approval. This check can only escalate, never clear |
| P1 | `require` preference not met (lactose-free = `FREE_FROM_LACTOSE` claim or ML free-from; Finnish = origin `fi`; organic = organic label, or "luomu" in the name as weaker evidence) | needs approval with the best available option |
| P2 | `prefer` preference not met | pass, shown as a missed preference |
| S1 | Spend: line above line limit (default €250 net) | needs approval |
| S2 | Spend: cart above auto-order limit (default €1 500 net) or above the buyer's budget | needs approval |
| S3 | Cart above budget + 15 % | block until the buyer changes the plan |
| Q1 | Ordered quantity exceeds need by more than 25 % (pack rounding) | needs approval |
| Q2 | Pack size unknown | needs approval |
| D1 | No delivery on or before the needed-by date | needs approval (shows the earliest date) |
| D2 | Sales block, empty schedule, or no price | block for that product (the agent looks for another) |
| D3 | Stock below need | needs approval for a split or substitute |
| D4 | Closing time already passed | warning only (mock data, see findings) |
| X1 | Substitute from Valio's substitution list that passes all rules, price within +15 % | pass, counted |
| X2 | Substitute found by the agent's own search | needs approval |
| X3 | More than 3 substitutions in one cart | the rest need approval |

Ordering mode: the cart is ordered as one order once every approval item is resolved. Rejected lines are left out and listed in the order result. Reason: one delivery per event, and the buyer sees one clear outcome.

## 4. Product matching and good / better / best

Only candidates that pass the hard filters (A1, D2, `require` preferences where any candidate meets them) are ranked.

Score per candidate, 0 to 100:

- Preference fit, 40: each `prefer` preference met. Label evidence scores higher than name-only evidence.
- Price, 30: comparison price per kg or l against the cheapest candidate.
- Pack fit, 15: low waste after rounding to whole sales units.
- Delivery reliability, 10: from the CSV. Missing data is neutral, not penalised.
- Carbon, 5: kg CO2e per kg against the other candidates. Missing data is neutral.

Tiers:

- **Good**: the cheapest candidate that meets every `require`.
- **Best**: the highest preference fit, ties broken by score.
- **Better**: the highest total score that is neither of the others.

The default pick follows the buyer's priority (`price` → good, `balanced` → better, `quality` → best). If fewer than three candidates exist, fewer tiers are shown. Reasons are written by code from the score parts ("Finnish origin (Blue Swan label), lactose-free claim, €6.13/l, 99 % delivery reliability"), so they always match the data.

The cart total shows net, gross and an estimated CO2e (kg CO2e/kg × kg ordered, from Valio's file, only where the data exists).

## 5. Exception handling

| Exception | Detection | Agent action | Rules |
|---|---|---|---|
| Partial stock | Stock amount below need | Split across the next-ranked product, or a substitute | D3 → approval with the split shown |
| Late delivery | `availableDate` after the needed-by date | Look for a candidate available in time | If none, D1 → approval with the earliest date |
| Unavailable | Empty schedule, amount 0, sales block | Next candidate, then Valio substitution list, then agent search | D2, X1 to X3 |
| Unmet preference | No candidate meets a `require` | Best available option with what is missing | P1 → approval |
| Allergen risk | MAY_CONTAIN or missing data | Prefer candidates with full data | A2, A3, A4 |
| Price missing | `error: true` | Drop the candidate | D2 |
| Over budget | Cart total | Propose a cheaper tier per line, biggest savings first | S2, S3 |
| LLM failure | Invalid output after retry | Fail the step with a clear message | Run status `failed`, audit entry |

## 6. Audit log

- Append-only `AuditEntry` per event: sequence number, timestamp, actor (agent, rules, human, system), action, a **human-readable summary sentence**, optional line and product refs, rule ids, model id, structured details.
- Tamper evidence: each entry stores `prevHash` and `hash` (SHA-256 over the entry). The UI can show "chain verified".
- `AuditStore` and `RunStore` interfaces with three implementations: in-memory (tests), JSON file in `.runtime/` (default, git-ignored), Aiven PostgreSQL (stretch). Chosen by `STORE=memory|file|postgres`.
- Export: `GET /api/runs/:id/audit?format=json|md` returns the whole trail as JSON or a readable Markdown report.

## 7. Openness and path to production

- **Our HTTP API** documented as OpenAPI 3.1, generated from the zod schemas, served at `/api/openapi.json`, with notes in `docs/API.md`.
- **MCP server** at `/api/mcp` (Streamable HTTP) so an external purchasing agent can use Limos: `search_products`, `check_price_and_availability`, `plan_event`, `get_run`, `submit_cart` (rules-gated). Approval stays a human action and is not an MCP tool.
- Standards used: OpenAPI, MCP, JSON Schema, GS1 allergen and label codes, ISO 3166 country codes, ISO 8601 dates.
- To production (in `docs/ARCHITECTURE.md`): customer-specific prices and availability via a real customer id, a real order API behind `OrderService`, per-customer policy files, auth and approver roles, PostgreSQL, scheduled re-checks before the cut-off.

## 8. Phases

Gate for every phase: `npm run check` (typecheck + lint + vitest). Each phase is about 1 to 2 hours. The target is a thin end-to-end flow by Saturday morning.

UI track (Johannes + designer) runs in parallel from the start against `fixtures/contract/` and switches to the live API with `NEXT_PUBLIC_USE_FIXTURES=false` after Phase 1. Pages: event request, run progress with live audit, cart review with good / better / best, approvals, order result, audit trail.

### Phase 0: Scaffold and data layer

1. STANDARD: Next.js + TypeScript app at the repo root (`app/`, `lib/`, `types/`), scripts `dev`, `build`, `typecheck`, `lint`, `test`, `check`. Vitest. MIT `LICENSE`. `.env.example` with `VALIO_API_KEY`, `VALIO_BASE_URL`, `GEMINI_API_KEY`, `GEMINI_PRO_MODEL`, `GEMINI_FLASH_MODEL`, `STORE`. `.runtime/` in `.gitignore`.
2. ENGINE: `scripts/build-catalog.ts` reads `data/` and writes `data/catalog.slim.json` with normalised fields (names, origin, Finnish and organic evidence, allergens as contains / may contain / free from, claims, units, pack size, lot size, substitutes, reliability, CO2). `lib/catalog` loads it once and provides token search. Tests use small invented products, never real data.
3. STANDARD: `lib/valio` server-only client for ping, price and availability. Batches of at most 1 000, 5-minute cache per (endpoint, date, id), retry with backoff on 429 and 5xx, sentinel normalisation (999 999 and above = unknown or ample). `scripts/probe-valio.ts` for a live check. Tests with mocked fetch.
4. STANDARD: `lib/llm` Gemini wrapper: model ids from env, `generateStructured(schema, prompt)` with validation and one repair retry, function-calling loop helper, fake LLM. `scripts/list-models.ts` pins the current Pro and Flash ids.
5. TEST: `lib/contract/schemas.ts` (zod mirror of `types/contract.ts`) and a test that every fixture in `fixtures/contract/` parses.

### Phase 1: Thin end-to-end (target: Saturday morning)

1. ENGINE: `lib/rules` v1 with the policy file and rules A1, A3, P1, P2, S1, S2, D1, D2, D3. Pure functions, one test per rule.
2. ENGINE: `lib/agent/run.ts` pipeline with a fixed ingredient plan (no LLM yet): retrieval → simple top pick → price and availability → quantities → rules → approval items or simulated order. `lib/store` memory and JSON file stores with the hash chain. `lib/orders` simulated order service.
3. STANDARD: routes `POST /api/runs`, `GET /api/runs/:id`, `POST /api/runs/:id/approvals/:approvalId`, `GET /api/runs/:id/audit`, all returning contract shapes validated by zod.
4. TEST: end-to-end test with mocked Valio and the fake LLM, plus `npm run smoke` against the live API.

### Phase 2: Gemini planning

1. ENGINE: request parsing with Flash (fills blanks only).
2. ENGINE: recipe expansion with Pro: per-serving quantities, scaling, waste factor and merging in code, sanity bounds as assumption warnings.
3. ENGINE: relevance check with Flash over the top 20 candidates, ids validated against the list.

### Phase 3: Exceptions and approvals

1. ENGINE: exception detection and handling for partial stock (split), late delivery, unavailable, unmet `require` preference, allergen risk; rules A2, Q1, Q2, D4.
2. ENGINE: substitutions: Valio list first, then the Flash function-calling substitute finder; rules X1 to X3; every proposal goes through the rules.
3. STANDARD: approval resolution: re-evaluate, re-check availability before ordering, log any change, place the order when all items are resolved.

### Phase 4: Ranking

1. ENGINE: scoring and good / better / best tiers with reliability and carbon as secondary signals, code-written reasons, cart totals with CO2e, budget rules S3 and the "cheaper tier" proposal.

### Phase 5: Openness and docs

1. STANDARD: OpenAPI 3.1 generated from zod at `/api/openapi.json`, `docs/API.md`.
2. STANDARD: MCP server at `/api/mcp` with the tools in section 7.
3. COPY: `README.md` (setup, demo script, standards), `docs/ARCHITECTURE.md`, `docs/POLICY.md`.

### Phase 6: Demo hardening

1. TEST: two scripted scenarios from live data. A: small order within authority → agent orders on its own. B: wedding for 120 with partial stock, a late item, an unmet preference and a substitution → approvals → order.
2. STANDARD: resilience: record and replay of Valio and Gemini responses in `.runtime/` for an offline demo, a clear message when a limit is hit, audit export as Markdown.

### Stretch (only after Phase 6)

- S1 STANDARD: menu photo upload, Gemini multimodal reads dishes into the request form.
- S2 ENGINE: Gemma second-opinion allergen check over key ingredients text (rule A4). It can only escalate.
- S3 DB_RLS: Aiven PostgreSQL `RunStore` and `AuditStore`. Agents write the migration file, Mikael applies it.

## Rules for every phase

- `data/`, `.env*` and `.runtime/` are never committed, printed or copied. Tests use invented product records only.
- Keys stay server-side. No `NEXT_PUBLIC_*` secrets. Valio and Gemini calls happen only in `lib/` on the server.
- The LLM never decides whether to order. Rules in `lib/rules` decide, and every decision has a rule id and a reason in the audit trail.
- API responses match `types/contract.ts` and are validated by `lib/contract/schemas.ts`. Changing a shape means updating the contract doc, the types, the schemas and the fixtures in the same change.
- Query Valio only for shortlisted products, never the whole catalog.
- All code is written during the event and licensed MIT.

## Open questions

- Organisers: is a `closingTime` in the past for Terminal products intended? (Treated as a warning.)
- Organisers: does availability change during the event? (We re-check before ordering.)
