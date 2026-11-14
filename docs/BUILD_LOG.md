# Build log

How Limos was built, step by step. Each phase in `docs/PLAN.md` runs through the same loop: Claude Code plans the step and writes the prompt, Codex CLI builds it in a sandbox with no network or git access, the repo gate runs (ruff, ruff format, mypy strict, pytest), and a model from another family (Gemini through Antigravity) reviews the diff against the plan. A FAIL sends the step back to a fresh Codex session with the issues. Claude commits only after a PASS.

- **Fix rounds** counts builds after the first, whether a gate failure or a review FAIL caused them.
- **Tests** is the number of tests in the suite after the step's commit (`pytest --collect-only`).
- Review levels: *standard* (`gemini-3.8-flash-medium`) for STANDARD and TEST steps, *strict* (`gemini-3.8-flash-high`, high effort) for ENGINE steps and copy.

## Phase 0: Backend scaffold and data

| Step | Tag | What was built | Builder | Reviewer | Fix rounds | Tests | Commit |
|---|---|---|---|---|---|---|---|
| 1 | STANDARD | FastAPI scaffold: health route, contract error format, CORS, uv project, MIT licence | Codex gpt-6.1-sol | Gemini 3.8 Flash, standard | 1 (mypy error caught by the gate) | 9 | 32cd37d |
| 2 | STANDARD | Valio client with batching, retry and backoff, TTL cache, stock sentinel handling, fetch and ping scripts | Codex gpt-6.1-sol | Gemini 3.8 Flash, standard | 0 | 54 | 096144f |
| 3 | ENGINE | Slim catalogue build, labels, allergens, pack sizes, deterministic token search | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 1 (real-data build crashed on a zero volume) | 204 | f4d8688 |
| 4 | STANDARD | Gemini wrapper: structured output with one repair retry, validated tool loop, offline fake | Codex gpt-6.1-sol | Gemini 3.8 Flash, standard | 0 | 229 | b532877 |
| 5 | TEST | Contract schemas, fixture round-trip tests, OpenAPI snapshot | Codex gpt-6.1-sol | Gemini 3.8 Flash, standard | 0 | 267 | b3aba95 |

## Contract v0.3: Valio mentor requirements

| Step | Tag | What was built | Builder | Reviewer | Fix rounds | Tests | Commit |
|---|---|---|---|---|---|---|---|
| 1 | TEST | Customers, reorder, clarifying question, suggestions and per-line deadlines in schemas, fixtures and the OpenAPI snapshot | Codex gpt-6.1-sol | Gemini 3.8 Flash, standard | 1 (restored renumbered v0.2 product ids) | 332 | 6014801 |

## Phase 1: Thin end-to-end

| Step | Tag | What was built | Builder | Reviewer | Fix rounds | Tests | Commit |
|---|---|---|---|---|---|---|---|
| 1 | ENGINE | Offers engine: unit and lot conversion, delivery date and order deadline per line, dietary checks, flags | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 0 | 419 | 93ac02c |
| 2 | ENGINE | Policy engine with mandates and every guardrail rule except price change | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 0 | 535 | e24ad37 |
| 3 | ENGINE | Price-locked quotes, hash-bound approvals, idempotent orders, store, hash-chained decision log | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 0 | 581 | 2508e22 |
| 4 | ENGINE | Seeded customers, profile defaults with filled gaps, reorder with guest scaling | Codex gpt-6.1-sol (first run interrupted, resumed) | Gemini 3.8 Flash, strict | 0 | 607 | 8c347e0 |
| 5 | STANDARD | Contract routes and a deterministic runner for item lists and reorders | Codex gpt-6.1-sol | Gemini 3.8 Flash, standard | 0 | 651 | 6bc58fc |
| 6 | TEST | End-to-end tests over HTTP and a live smoke script | Codex gpt-6.1-sol | Gemini 3.8 Flash, standard | 0 | 656 | 1f45936 |

## Phase 2: MCP server

| Step | Tag | What was built | Builder | Reviewer | Fix rounds | Tests | Commit |
|---|---|---|---|---|---|---|---|
| 1 | STANDARD | MCP server on the official SDK over the gateway's REST tools, no approve tool | Codex gpt-6.1-sol | Gemini 3.8 Flash, standard | 0 | 671 | fdcf226 |
| 2 | TEST | MCP client session tests: AUTO order, approval held until a human approves over REST | Codex gpt-6.1-sol | Gemini 3.8 Flash, standard | 0 | 680 | fcec345 |
| 3 | COPY | `docs/MCP.md` and a plain REST client (copy by Claude) | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 0 | 686 | 9503366 |

## Phase 3: Gemini agent

| Step | Tag | What was built | Builder | Reviewer | Fix rounds | Tests | Commit |
|---|---|---|---|---|---|---|---|
| 1 | ENGINE | Free-text intent with Gemini Flash, gap filling by precedence with sources, free-text reorder | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 0 | 718 | 8c4ea85 |
| 2 | ENGINE | One clarifying question decided in code and worded by Gemini, answer route with "Use default", resume after the answer | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 1 (gate: delivery default changed without a model; two step 1 test cases superseded by the question) | 749 | e64f4f9 |
| 3 | ENGINE | Event expansion: Gemini proposes ingredients per serving; code scales by servings, adds mandate waste, normalises units, merges across dishes and adds sanity notes; Flash vs Pro eval script | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 1 (gate: three earlier Phase 3 test cases had no expansion output) | 777 | 01b5298 |
| 4 | ENGINE | Gemini tool loop over HTTP: picks candidates and requests the quote through the gateway REST tools as agent limos-gemini; code fills ids, quantities and constraints, validates every choice and checks the explanation against the facts | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 0 | 796 | a95dcbd |
| 5 | STANDARD | Fix found in a live check: Gemini rejected `additionalProperties` and `maxItems`, so every structured call failed with 400; Gemini now gets a reduced schema while Pydantic still validates in full | Codex gpt-6.1-sol | Gemini 3.8 Flash, standard | 1 (stopped on three Phase 0 assertions that pinned the old call; those three were allowed to change) | 808 | 23bd723 |

## Between phases: Postgres store (Johannes) and review fixes

| Step | Tag | What was built | Builder | Reviewer | Fix rounds | Tests | Commit |
|---|---|---|---|---|---|---|---|
| Merge | DB_RLS | Johannes's PostgresStore with database-level guarantees, catalogue-price rule, hosting files; merged from the web branch with the psycopg dependencies | Johannes (by hand, with his own tools) | Gemini 3.8 Flash, strict: FAIL | 1 (next row) | 816 | 43ea418 |
| Fix | DB_RLS | No migration on start unless `MIGRATE_ON_START`, a migration command, decided approvals final (migration 002), a thread guard for store transactions | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 0 | 843 | 5fee80e |
| Allergen gap | ENGINE | Every quote gets the buyer's own profile rules (excluded allergens, required dietary keys), also standalone MCP and script quotes; optional quote fields only add restrictions; contract v0.4 | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 0 | 863 | 0d11ff1 |
| Pena approves | ENGINE | Mandate setting `always_require_approval` (on for Ab Oy): every non-blocked quote needs one-tap approval via `APPROVAL_ALWAYS_REQUIRED`; the chef keeps AUTO within the mandate; contract v0.5 | Codex gpt-6.1-sol | Gemini 3.8 Flash, strict | 1 (stopped on a mandate test that pinned identical mandates; one line allowed to change) | 877 | see git log |
