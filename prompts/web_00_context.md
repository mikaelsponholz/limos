# WEB PROMPT 0: Limos web app

You are the frontend builder for **Limos**, our Junction 2026 hackathon project. Mikael builds the backend (FastAPI gateway, Gemini agent, Aiven) in parallel. You build the web app in `web/`, against the **fixtures first**, and switch to the real backend later with one env variable.

Before writing code, read in this order:
1. `docs/PLAN.md` (the whole project: flow, guardrails, presets, exceptions)
2. `docs/api_contract.md` v0.2 (the contract with the backend; this is the source of truth for every shape)
3. `fixtures/api/*.json` (realistic mock data for every shape, invented products with ids `9000xx`)

## 1. Your zone and rules
1. **Only edit files under `web/`.** Mikael owns everything else. `docs/api_contract.md`, `fixtures/api/` and `contract/openapi.json` change only by agreement: if you need a change, list it in your report instead of editing.
2. Never commit `.env*` files with values, and never touch `data/`.
3. The web never calls Valio or Gemini. It only talks to the Limos backend (or the fixture mock).
4. Every screen handles loading, empty and error states.
5. Demo first: the four presets must run smoothly on fixtures before anything fancy.

## 2. What Limos is (short)
A kitchen describes an event (or picks a preset, an item list or free text). The Gemini agent expands it into items, finds products, and picks good / better / best options. The gateway locks prices into a quote and deterministic rules decide: `AUTO`, `NEEDS_APPROVAL` or `BLOCK`. A human approves from their phone when needed. Every step lands in a tamper-evident decision log. Orders are **simulated** (the Valio mock API has no order endpoint), and the UI says so plainly.

Pitch: **"The AI proposes, the rules decide, the human signs."** The value for the kitchen is time saved on routine search, comparison and ordering. Never write speed promises or numbers we have not measured.

The UI is what the judges see. 30% of the Valio score is a working end-to-end demo and 20% is visible guardrails, so the flow and the controls must be obvious at a glance.

## 3. Screens

### A. Kitchen buyer (desktop / tablet)
1. **New request** (`/`)
   - Four **preset cards** from `GET /presets` (title, description, "what should happen"). One click starts the request.
   - Below: a simple **event form** (type, date, guests, dishes), preference chips (`lactose_free`, `gluten_free`, `finnish_origin`, `organic`, `vegetarian`, `vegan`, `low_carbon`, each with require / prefer), excluded allergens, budget, priority (price / balanced / quality), deliver-by date, and a free-text field.
   - Buyer selector (default `demo-catering`).
2. **Request in progress** (`/requests/[id]`), polling `GET /requests/{id}` every 1.5 s while `RUNNING`:
   - **Live timeline** of `steps`: actor icon (user, agent, gateway, policy, human), plain `title`, time, expandable `detail`. Mark steps with `model` set as "Gemini".
   - **What the agent understood**: event, dishes, items (with fi name and which dish uses them), preferences, budget and `assumptions` (e.g. "5 % waste added").
   - **Items and options**: per item, the good / better / best `candidates` side by side (price per L/kg, line total, available date, reliability, CO2, labels, allergens, `dietary_check`, flags, `reason`). The chosen one highlighted. Substitutes labelled "Valio-approved substitute" or "AI-proposed substitute". Split lines shown together.
   - **Quote card**: total excl. / incl. VAT, CO2 total, budget status, expiry countdown, and the **policy outcome as a big badge** (green AUTO, amber NEEDS APPROVAL, red BLOCKED) with each fired rule in plain language.
   - **Agent explanation** (`agent_explanation`).
   - **Final state**: order confirmed (with a visible "Simulated order" tag and the `authority` sentence), waiting for approval (link to the approval), blocked or rejected (with the rule), or failed (with `error`).

### B. Approver (phone first, 375 px)
3. **Approvals inbox** (`/approve`): pending approvals, newest first: summary, reasons, expiry.
4. **Approval card** (`/approve/[id]`):
   - What is being ordered, total incl. VAT, delivery date.
   - **Why approval is needed**: fired rules in plain language.
   - Agent explanation.
   - **Options** (up to 3 quotes, recommended first) as selectable cards with their `option_label`.
   - Big **Approve** / **Reject** buttons. Approve sends `quote_id` + `content_hash`. Handle `409 QUOTE_CHANGED` ("This quote changed after you opened it. Reload.") and `410 QUOTE_EXPIRED` ("Expired. The agent will re-quote.").
   - Approver name remembered in localStorage.

### C. Decision log
5. **Audit** (`/audit/[requestId]`): the full log as a clean vertical timeline readable by a non-technical person, with a **"Chain verified" / "Chain broken"** badge from `chain_valid`, and a "Copy as JSON" button.

### D. Frame
- Header: "Limos", nav (New request, Approvals with a pending count badge, "Open spec" linking to the backend's `/openapi.json`).
- Footer: "Prices and availability from Valio Aimo mock APIs. Orders are simulated. Policy decisions are deterministic code, not AI."

## 4. Tech
- **Next.js (App Router) + TypeScript + Tailwind CSS** in `web/`. shadcn/ui is fine.
- **Types:** when `contract/openapi.json` exists, generate them with `openapi-typescript` into `web/lib/api-types.ts` (add an npm script `gen:types`). Until then, write `web/lib/types.ts` by hand from `docs/api_contract.md`, matching the fixtures exactly, and note it in the report.
- **API client** `web/lib/api.ts` with one function per endpoint.
- **Mock mode:** `NEXT_PUBLIC_USE_MOCK=true` serves responses built from `fixtures/api/` (copy or import them into `web/`; do not edit the originals). Simulate a run: steps appear one by one over about 6 seconds, then the request reaches its final state. An approval can be approved from `/approve`, and the request then shows the order. Include a way to trigger the 409 case.
- Otherwise calls `NEXT_PUBLIC_API_BASE` (e.g. `http://localhost:8000`).
- Responsive. Test the approval card at 375 px.
- Calm, trustworthy look: white background, one accent colour, clear status colours (green / amber / red), big readable numbers, no noisy dashboards. English UI.

## Your task for this prompt
1. Read the three sources above and confirm in max 8 bullets what you will build.
2. Scaffold `web/` (Next.js + TS + Tailwind) with `web/.env.example` containing `NEXT_PUBLIC_API_BASE=http://localhost:8000` and `NEXT_PUBLIC_USE_MOCK=true`.
3. Build the types, `lib/api.ts`, the fixture-based mock, and the screens **New request** and **Request in progress**, working in mock mode for the `wedding_120` preset.
4. Stop there. Approvals and audit come in the next prompt.

**Report back:** the file tree of `web/`, how to run it, a short description (or screenshots) of the two screens for `wedding_120`, and any contract changes you would propose.
