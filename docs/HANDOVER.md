# Handover to Johannes: search and answers

From Mikael, 2026-11-14. You take over search and answer quality from here. Please start from branch `fix-pick-quality`, not from `main`. It already contains your two latest main commits (cheesecake seed, scalable reorder), and it fixes most of what goes wrong in search today. Starting from main would redo this work and make our changes collide.

```bash
git fetch origin
git switch fix-pick-quality
```

The branch passes the gate with 1092 tests. One Postgres guard test sometimes errors on connection setup; it passes when run alone. Open a PR into main from this branch when you're ready, or tell Mikael to.

## Where things stand

- **Phase 4** (exceptions, presets) is merged.
- **Feature freeze:** since then we only fix what the live demo runs show. No new features.
- **Phase 5** (ranking) is out.
- **Next:** Phase 6 (deploy, docs, demo hardening).

## What the branch fixes (please build on it)

Live, our own agent used to quote wrong products:
- "egg" became egg noodles;
- lactose-free cheese became Roquefort;
- approval options offered chilli paste for sugar paste.

The branch fixes this. In the live run, the chef's orders get real eggs, lactose-free milk, butter and potatoes.

1. **Catalogue search** (`gateway/catalog/index.py`):
   - Tokens drop English plurals, so "egg" matches "eggs".
   - Ties break by the more specific product name, no longer by product code.
   - This also changes search for MCP and tool-loop callers.
2. **Search terms** (`agent/expansion.py`): each item gets 1 to 2 terms, one of them Finnish where there is a clear Finnish name, and never preference words. Many products only have a Finnish name.
3. **Pick and suitability** (`agent/relevance.py`, `agent/pipeline.py`):
   - One structured Gemini call picks a product per item, or `null` when nothing is the item itself. It also lists the other suitable candidates, Valio substitutes included.
   - Code validates every id.
   - Missing picks get one repair call. A fallback is taken only when the product name contains every word of the search term.
   - An item with nothing suitable is left out with "No suitable product found for the X; a person needs to choose one."
4. **Alternatives** (`agent/exceptions.py`, `agent/purchasing.py`): splits, approval options, cheaper options and substitutes come only from suitable products.
5. **Approvals:** substitute lines always keep `substitute_for` and `substitute_source`, so the `SUBSTITUTION` approval can't be skipped. Two regression tests cover this.
6. **Rule noise** (`gateway/policy/engine.py`): an unknown pack size no longer also fires `PARTIAL_QUANTITY` with 0 coverage.
7. **Demo case finder** (`scripts/find_demo_cases.py`):
   - Incomplete Valio data marks that row `offer_data_invalid` and the run continues.
   - A failure prints the error type.
   - `--scan-substitutes` finds real shortages that have an in-stock Valio substitute.

How the agent works:
- **Pipeline (default):** `AGENT_MODE=pipeline`. One expansion call, one batched Valio call and one pick call, with code doing the rest.
- **Tool loop:** `AGENT_MODE=tools` keeps the old Gemini tool loop for external agents (MCP).

## Overlap with your work

These files are the core of the search and answer flow, and the branch changes them:
- `gateway/catalog/index.py`
- `agent/expansion.py`
- `agent/relevance.py` (new)
- `agent/pipeline.py`
- `agent/exceptions.py`
- `agent/purchasing.py`

Your `agent/intent.py` change merged cleanly. Mikael and Claude will stay out of these files from now on.

## Open items

1. **Frank's demo shortage.** The video should show Frank's "same as last time for the wedding, 80 people, Saturday" with a stock shortage plus a sensible Valio substitute.
   - **Seed now:** one order with your cheesecake plus 6 × Oatly iKaffe 1L for 30 guests (`customers/c_pena.json`, `scripts/customer_seed_spec.json`).
   - **Problem:** Oatly isn't short live (39 243 in stock), and no cake has a Valio substitute.
   - **Live scan:** the only shortage realistic for 80 guests is Santa Maria green peppercorns 13 g (stock 3) → Salliselta green pepper whole 160 g (ample stock). The others need hundreds of kilos.
   - **Not decided:** use peppercorns, or show Frank's reorder without a shortage.
2. **Live check.** After any seed change:
   - Re-seed with `python scripts/seed_customers.py --catalog data/valio/catalog.slim.json`.
   - Restart the gateway.
   - Run Frank's request and the chef's weekly order live.
   - The finder needs `VALIO_BASE_URL` and `VALIO_API_KEY` in the shell: `python scripts/find_demo_cases.py --live [--scan-substitutes]`.
3. **Known limits**, also in PLAN.md open questions:
   - **Agent substitute:** it is dropped (the item shows "not found") when a moved default date re-prices offers and the original's data then turns out incomplete. Safe direction: it never orders without approval.
   - **Chef's 40 L milk:** it becomes one 1 l line, because Valio gives no pack size. `PACK_SIZE_UNKNOWN` flags it for approval, and it's not in the video.
   - **Suggestions:** Gemini proposes none for the chef, so they're out of the demo.
4. **web/** still says "Pena" in `HANDOFF.md`, `README.md`, `lib/mock.ts` and a comment in `components/OrderLine.tsx`. Frank is the name everywhere else. The ids stay `c_pena` and `ab-oy`.
5. **Contract v0.6:** `QuoteLine` has `substitute_for` and `substitute_source`, and an item may have two lines (the second `is_split`).

## Notes on how this was built

The pick-quality step was built by Codex. Its reviews ran as fresh read-only Codex sessions, because the Gemini reviewer was blocked by a permission rule, so they were not cross-family. It needed two fix rounds and one escalation. The last finding was accepted as a known limit (open item 3, the agent substitute). The Frank seed and finder commit is gated but not reviewed. Build log rows are in `docs/BUILD_LOG.md`, and decisions are in `docs/PLAN.md`.
