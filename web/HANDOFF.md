# Handoff: Limos web UI (for Anna)

Welcome! You now own the UI in `web/`. This page is everything you need to start. The longer design brief is `prompts/web_00_context.md`, and the API is `docs/api_contract.md` (v0.3).

## The project in 30 seconds

Team **Fundable**, project **Limos**, for the Valio Aimo "Trusted Trade Agents" challenge (Junction 2026), plus the Gemini and Aiven side challenges.

An AI purchasing agent buys food from Valio Aimo. The gateway locks prices into a quote. Fixed rules (not AI) decide: **AUTO** (order straight away), **NEEDS_APPROVAL** (a person taps "Approve order") or **BLOCK** (can't be ordered, e.g. an allergen). Every step goes into a hash-chained decision log stored in Aiven PostgreSQL that can't be edited.

| Who | Owns |
|---|---|
| Mikael (Mikke, writes in Swedish) | Backend: `gateway/`, `agent/`, `mcp_server/`, the contract and fixtures |
| Johannes | Aiven storage (`gateway/store/`), pitch, coordination |
| Anna | The UI in `web/` |

**Branch:** work on `web`. Mikael merges `web` into `main` after his phase 3. Pull `origin/main` into `web` when he pushes. Don't edit backend files without telling Mikael, his Claude Code is working in `main`.

## Who the UI is for

1. **Pena, 65, busy executive** (`c_pena`). Design for him first: big text, plain English, one big lime "Approve order" button, a shopping list with delivery date and order deadline on every line. **Pena always confirms with one tap** (agreed with Mikael).
2. **The chef** (`c_chef`, Alex). Playful: sees optional seasonal ideas under "If you want". **The chef's routine weekly order goes AUTO** within the restaurant's mandate. The demo shows both sides.
3. **An approver** for someone else's order (`/approve`, phone first).

Design rules (approved): friendly grocery app, **nothing that looks "AI"** (no chat bubbles, sparkles or gradients). Colours in `tailwind.config.ts`: page `#F6F4EF`, petrol `#005670`, lime `#C0E021` only for "Approve order". Font Figtree, 18 px base.

## Screens (all built)

| Route | What |
|---|---|
| `/` | "Hi Pena! Same as last time?", text field, numbered past orders, then the list with "We assumed" lines, rule warnings and "Approve order" |
| `/approve`, `/approve/[id]` | Approvals inbox and card (options, 409 "prices changed" flow) |
| `/settings` | Text size, price vs quality, likes, allergens, budget, demo customer switch |
| `/audit/[id]` | Order details: the decision log and "nothing has been changed" check |

Key files: `lib/api.ts` (one function per endpoint), `lib/mock.ts` (fake backend), `lib/types.ts`, `components/OrderLine.tsx` (one list row, plus `reasonLabel` for rule names), `lib/settings.ts`.

## Run it

**Mock mode** (no backend, fixtures only), the quickest way to work on design:

```bash
cd web
cp .env.example .env.local
npm install
npm run dev -- --port 3020
```

**Real backend** (needs keys from Johannes or Mikael, never commit them): repo root `.env` with `VALIO_API_KEY`, `VALIO_BASE_URL`, `GEMINI_API_KEY`, `DATABASE_URL`. Then:

```bash
python scripts/build_catalog.py                       # once; needs Valio files in data/valio/
python scripts/seed_customers.py --catalog data/valio/catalog.slim.json
uvicorn gateway.main:app --port 8000 --env-file .env  # with STORE=postgres and WEB_ORIGIN=http://localhost:3020
```

and in `web/.env.local`: `NEXT_PUBLIC_USE_MOCK=false`, `NEXT_PUBLIC_API_BASE=http://localhost:8000`.

## State right now (Sat 14 Nov)

Works against the **real** gateway: customers and order history, "Order again" (reorder), quote with real Valio prices, AUTO order, order details with the real log.

Not available yet: **free text** ("cakes for 30 on Friday") needs Mikael's phase 3 (Gemini). Until then free text only works in mock mode.

## Your first tasks, in order

1. **Check "Approve order" against the real gateway.** It failed with 422 because the gateway requires `comment`. Fixed in `lib/api.ts` (a448059) but not re-tested. Log in as the chef, tap "Weekly pantry staples", then "Approve order".
2. **"We assumed" lines show raw gateway text**, e.g. "Buyer bistro-example, from Alex Example's profile" and "Preference organic: prefer". Hide the `buyer_id` line and turn `preferences.*`, `priority` and `deliver_by` into plain words (see `filled_gaps[].field` and `.value`). Mock lines already read well.
3. **Loading feels stuck.** A real reorder takes about 7 to 20 s (Valio calls). "Checking prices…" needs to feel alive: say what is happening, keep the old list visible.
4. **Approvals inbox and card against the real gateway** (`/approve`). Not tested with real data yet.
5. **Settings against the real gateway**: the request sends `preferences`, `exclude_allergens`, `priority`, `budget`. Check the gateway respects them (e.g. "never tree nuts" should block a nut product).
6. **Phone width (375 px)** for every screen, and "Large" text size.

## Known backend issues (tell Mikael, don't fix in `web/`)

- **Pena goes AUTO** in the real gateway, but we agreed he always confirms. Policy change on Mikael's side.
- **Allergen gap over MCP**: an outside agent can quote around a customer's allergen rules. Mikael fixes it after phase 3. This is what the judges will test.
- Product names come from Valio in mixed English and Finnish ("RollfoodsStrawberry Cheese cake"). That is the data.

## Rules

- Never commit `.env`, keys, `DATABASE_URL` or anything in `data/` (Valio's data must not be public).
- All people and companies in fixtures are invented. Keep it that way.
- UI text in English, written for Pena: short, friendly, no jargon.
