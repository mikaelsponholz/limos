# Handover to Mikael: robustness fixes and chat revision

From Johannes, 2026-11-14. Mikael takes over everything below, including the search and answer files `docs/HANDOVER.md` gave to Johannes. This file is written for Mikael's Claude.

## Where the work is

- **Branch:** `fix-pick-quality`, on top of `19f1da3` ("Add the handover for Johannes").
- **State:** everything below is in one commit after that one. Nothing is merged into `main`.
- **Gate:** ruff and mypy are clean.
- **Tests:**
  - The last full run was 1081 passed and 1 failed.
  - The failure was `test_presets_match_fixtures_and_overrides_win`. It was fixed by adding `"revises": null` to `fixtures/api/presets.json`, and the contract tests pass now (152).
  - The full suite was not rerun after that fix. Run it first.
  - Run it without `tests/test_engine_store_audit.py`, because that Postgres test hangs over the network here.
- **Feature freeze:** part 2 is a new feature, which breaks the freeze in PLAN.md. Johannes asked for it, because Valio Aimo staff want chat changes. Decide whether it ships.

## 1. Robustness fixes (Valio Aimo staff requirements)

The requirements: busy customers, many of them aged 65, who write short or vague text, spoken Finnish or English, and who do not want to be asked questions.

- **`gateway/runner.py`:**
  - The customer profile is loaded before `parse_text` and passed to it.
- **`agent/intent.py`:**
  - `TextItem.quantity` and `unit` are optional, so "maitoo ja kermaa" no longer triggers a question.
  - An item with no amount becomes 1 pcs, with the assumption "No amount was given for X; quoting one pack. Change it before approving if you need more."
  - `parse_text(..., customer)` sends the titles and event types of the 5 latest past orders.
  - The prompt now covers spoken Finnish and typos ("maitoo", "huomiseks", "sama ku viimeks") and English.
  - A vague request for a product kind that a past order title names is treated as a repeat of that order.
- **`agent/question.py`:**
  - The `deliver_by` question was removed. A missing date is never asked. It defaults to the next delivery day and moves to the earliest date Valio offers.
- **Tests:** `test_intent.py` and `test_question.py` have new tests. `test_pipeline.py` and `test_pipeline_substitute_refresh.py` were rewritten to the no-question behaviour; none were silenced.

## 2. Chat revision of a request (new feature)

**Goal:** the buyer changes an open request in chat. Example: "strawberry cake for 20" then "make it blueberry muffins instead, 120".

- **`RequestCreate.revises: str | None`** (`gateway/schemas.py`):
  - It needs `text`, otherwise the request fails validation with 422.
  - It may omit `buyer_id` and `customer_id`, which are inherited from the earlier request.
- **`gateway/runner.py`:**
  - **`_revised_request`:**
    - The earlier request must exist and belong to the same buyer and customer, otherwise 404.
    - Its status must be in `REVISABLE` (NEEDS_INPUT, WAITING_APPROVAL, BLOCKED, REJECTED or FAILED). Otherwise it fails with 409 `NOT_REVISABLE`, because the request is still RUNNING or already ORDERED.
  - **`_replace`** runs in the same transaction as saving the new request:
    - A pending approval is rejected through `quotes.reject`, with approver "buyer" and reason "Replaced by the changed request r_…".
    - The old request becomes REJECTED, its question is cleared and its offered suggestions are declined.
    - When there was no pending approval, a `REJECTED` step with `{"replaced_by": id}` is appended instead.
    - Result: the old quote can never be ordered next to the new one.
  - **`run`:** passes `revision_context(previous)` to `parse_text`.
- **`agent/intent.py`:**
  - `revision_context` holds the earlier request's text, event, dishes, deliver_by, budget and explicitly named items, but not the expanded ingredients.
  - With that context, the prompt asks for the complete updated request: keep what the change does not touch, replace what it changes, and do not set `reorder.repeat` when new dishes or products are named.
- **Contract:** `contract/openapi.json` was regenerated with `scripts/export_openapi.py`, and the fixtures were updated.
- **Tests:** `tests/test_revise.py`, 3 tests:
  - the open quote is replaced and the old approval then returns 409;
  - an ORDERED request returns 409;
  - a revision with no text is rejected, a different buyer gets 404, and the original request is untouched.
- **Web:** not done (Anna). A chat message that changes an open request should POST `/requests` with `{"revises": <request_id>, "text": <message>}`.

### Live run (real Gemini and Valio, customer `c_chef`)

1. "Mansikkakakku 20 hengelle lauantaiksi 21.11." gave WAITING_APPROVAL with one strawberry cake product, 54.50 €.
2. "Vaihdetaan mustikkamuffineihin, tehdään 120 kpl." gave WAITING_APPROVAL with dish "mustikkamuffinit" for 120 servings, the same date and guest count, 64.89 €. The old request became REJECTED.
3. **Bug:** the muffin list has flour, sugar, butter, eggs, baking powder, vanilla sugar and salt, but **no blueberries and no milk**. It is not yet known whether expansion or the pick step dropped them. Check the `ITEMS_EXPANDED` and `EXCEPTION` steps of the revised request.

## 3. Open problems from the robustness rerun

The robustness rerun had 11 cases with live Gemini and Valio. Cases 03 and 06 wrote no result, so rerun them.

| # | Customer | Text | Result | Likely fix |
|---|---|---|---|---|
| 11 | c_chef | "kananmunia 200 kpl ja voita 10 kg" | BLOCKED, NO_USABLE_PRODUCTS | **Most important.** Finnish inflected forms reach search as is. Make the parser return item descriptions in base form ("kananmunia" to "kananmuna", "voita" to "voi"), or lemmatise in search. |
| 10 | c_pena | "mansikkakakku 20 hengelle ilman pähkinöitä" | Became a repeat of the lemon cheesecake order (plus Oatly) | The "product kind named by a past order title" rule in the `parse_text` prompt is too broad. It should apply only when the text names no specific product that differs from the title. |
| 08 | c_pena | "tarvitaan 30 henkilön lounas, kasvisvaihtoehto ja gluteeniton" | FAILED, NO_EVENT_ITEMS | An event with preferences but no dishes expands to nothing. Expansion should propose dishes first. |
| (revision) | c_chef | blueberry muffins for 120 | Blueberries and milk missing | See part 2. |

These cases worked:
- 01 "kakkua perjantaiksi"
- 02 the wedding for 80
- 04 "maitoo ja kermaa huomiseks"
- 05 the Christmas table for 40, 314.83 €
- 07 "sama ku viimeks"
- 09 "trendikästä jälkkäriä"

The many `OFFER_DATA_INVALID` exceptions are incomplete Valio data. Another product is then used, so they do not break orders.

## How to run things live

- Use `.venv/Scripts/python.exe` with `PYTHONPATH=.`.
- Inside the script, load only the `GEMINI_*` and `VALIO_*` keys from `.env` into `os.environ`, and never print them.
- Use `TestClient(create_app(Settings(store="memory")))` and set `client.app.state.gateway_http_client = client`.
- POST `/requests`, then poll `GET /requests/{id}` until the status is no longer RUNNING.
- One case takes about 30 to 150 s.

## Still open from `docs/HANDOVER.md`

- Frank's demo shortage. Not decided: use the peppercorns, or show his reorder without a shortage.
- `web/` still says "Pena" in a few files.
- PR from `fix-pick-quality` into `main`.
- After any seed change: re-seed, then run Frank's request and the chef's order live.
- Aiven redeploy after merge. A new container shows CATALOG_UNAVAILABLE for 1 to 2 minutes.

## 4. Price insights (branch `pricing-insights`, new feature)

Johannes asked for price optimisation from the customer's point of view. It is built on branch `pricing-insights`, which starts from `fix-pick-quality` at `0e3bd13`. It is not merged anywhere. It breaks the feature freeze, so decide whether it ships.

- **What it is:**
  - `RequestDetail.insights: list[PriceInsight]` (`gateway/schemas.py`). Each insight has kind, item_id, product_id, text and saving.
  - The field has a default of `[]`, so stored requests and fixtures stay valid. `contract/openapi.json` was regenerated.
- **Insights only inform.** They never change the quote or skip an approval.
- **Where it runs:** `gateway/runner.py` `_add_insights`, after the order or approval and before suggestions.
  - It runs inside a try block. A failure appends an EXCEPTION step "Skipped optional price insights" and keeps the quote.
  - `Settings.price_insights` (env `PRICE_INSIGHTS=0` turns it off) exists because the date check makes extra Valio calls.
  - The `tests/test_pipeline.py` `environment()` turns it off by default, because those tests count the purchase path's Valio calls. Pass `insights=True` to turn it on.
- **All logic is in `agent/pricing.py`, in code with no LLM:**

  | Kind | What it shows |
  |---|---|
  | `delivery_date` | The basket repriced on up to 3 earlier weekdays (price plus availability). It names the cheapest day with everything in stock. |
  | `price_change` | A product whose price changes by 3 % or more between those days and the delivery date. |
  | `season` | A Finnish produce calendar: fresh berries, asparagus, chanterelles and so on are flagged out of season (frozen hint for berries) or in season. Preserved products are skipped. It also adds a Christmas note for December events. |
  | `waste` | Leftover from pack sizes. Only the 3 costliest, at 3 € or more and 25 % or more of the pack. |
  | `pack_size` | Another pack of the same product line, where only the size differs in the name, that covers the need for less money. |
  | `discount` | One summary of how far the customer's prices are below Valio's list prices. A single line is called a sale only when its cut is at least 5 points deeper than the usual rate. In Junction data every product is 15 to 20 % below list price, which is the agreement, not a sale. |
  | `per_guest` | The cost per guest. |
  | `price_trend` | A comparison against earlier orders. It uses only orders with `source == "limos"`, because the seeded history has invented prices. |

- **Tests:** `tests/test_pricing.py`, 9 tests. The full suite passes with 1091 tests (without the Postgres test). Ruff and mypy are clean.
- **Live (real Gemini and Valio):**
  - Frank's wedding for 80: per guest 2.64 €, and customer prices 45.14 € below list price.
  - The chef's Christmas table: the Christmas note, two leftovers (vinegar 7.32 €, pickled cucumber 4.64 €) and per guest 1.17 €.
  - The chef's strawberry cake: an out-of-season strawberry note in an earlier run, when Gemini expanded it into ingredients.
  - `delivery_date` never fires live. A check of 200 random products priced on 6 dates over 45 days found no price changes.
  - 196 of those 200 prices are open-ended (`validTo` 9999-12-31) and 4 end on 2026-12-31. `price_change` therefore also names a price whose `validTo` is within 60 days of delivery ("set until 2026-12-31; a new price may apply after that").
- **Order:** `price_insights` returns the insights ranked for the chat: the cheaper delivery day first, then season, waste and pack size, then price changes and sales, then trend and per guest. The list-price summary comes last.
- **Web:** branch `pricing-insights-ui`, built on `ui-polish` at `594b210`. It is a separate worktree on Johannes' machine, `Desktop/limos-ui`.
  - `web/lib/types.ts` adds `PriceInsight` and an optional `insights` on `LimosRequest`.
  - `web/components/ConversationReply.tsx` shows the top 3 under "Price tips" in the latest reply. `saving` is shown as "save X €", or "X € unused" for waste.
  - Checked in the browser against a local gateway (memory store, live Valio and Gemini) with the chef's Christmas table: the Christmas note, vinegar 8.44 € unused and pickled cucumber 5.16 € unused, at phone width too.
  - tsc and eslint are clean.

### New bugs seen in these live runs (not price insights)

- "Joulupöytä 40 hengelle 19.12., kinkku ja rosolli" quoted delivery on **2026-11-16**, a month early. A missing deliver_by should default relative to the event date, not to tomorrow.
- The same request had **no ham line**, although "kinkku" was named.
