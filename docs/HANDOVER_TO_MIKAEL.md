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
