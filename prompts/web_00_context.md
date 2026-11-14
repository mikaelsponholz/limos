# WEB PROMPT 0: Limos web app

You are the frontend builder for **Limos**, our Junction 2026 hackathon project. Mikael builds the backend (FastAPI gateway, Gemini agent, Aiven) in parallel. You build the web app in `web/`, against the **fixtures first**, and switch to the real backend later with one env variable.

Before writing code, read in this order:
1. `docs/PLAN.md` (the whole project: flow, guardrails, presets, exceptions)
2. `docs/api_contract.md` v0.3 (the contract with the backend; this is the source of truth for every shape)
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

## 3. Who it is for, and the screens

**The user is "Frank"**: a busy, older business owner (think 65, not 25) who buys from Valio Aimo. Valio Aimo told us at Junction: their customers are in a hurry, want the order off their plate fast, and trust is everything. Today they phone Valio Aimo sales to ask about order cut-off times. So:

- **One text field first.** Frank writes what he needs, short and vague or long and precise ("strawberry cakes for 40 on Friday, same as last time"). Everything else comes after.
- **The answer is a shopping list and a total**, not a chat reply, not a dashboard.
- **The order cut-off is always visible**: "Order by Thursday 5 pm and it arrives Friday morning." Taken from the earliest `closing_time` and the quote `delivery_date`.
- **Suggestions only for playful customers** (`playful: true`, e.g. the chef, not Frank): below the list under "If you want", each with its price difference and an "Add" button (`POST /requests/{id}/suggestions/{sid}/accept`, which re-quotes). Never added automatically.
- **Recognise the user**: no sign-in. A customer picker (`GET /customers`, remembered in localStorage) is how the user is recognised. Greet by first name, show the company in the header, and offer "Order again" for the last past order (`reorder_of`).
- Requirements from `docs/PLAN.md` section 8b win if anything here disagrees.
- **Not "AI-looking"**: no chat bubbles, no sparkles or gradients, no robot avatar, no "As an AI…" voice, no "thinking" animations. It must look like a friendly grocery app.

### Design (approved, see the "Limos UI" canvas, screen 1)
- Warm light background `#F6F4EF`, white cards, **rounded corners** (cards 16 px, inputs 12 px, buttons pill-shaped), a very soft shadow `0 1px 2px rgba(31,42,48,.06)`.
- Brand: Valio Aimo petrol `#005670` for primary actions and links, lime `#C0E021` (text `#00394B`) only for the main **Approve order** button, soft green `#EAF4D3` for good news (cut-off banner, order placed). Text `#1F2A30`, muted `#6B767B`, borders `#E7E3DA`.
- Font: **Figtree** only, sentence case, no uppercase headings. Base size 18 px, field text 19 px, totals 30 px bold. Every tap target at least 44 px. Strong contrast (WCAG AA).
- English UI. Plain words: "Your list", "If you want", "Approve order", never "quote", "hash", "policy" or "agent" on Frank's screen.

### A. Order (`/`), the main screen
1. Header: "limos" wordmark in petrol, right side initials avatar + company ("FV · Frank Ab Oy").
2. Heading "Hi Frank, what would you like to order?" and the text field with a petrol **Make my list** button, plus an "Order again: <last order title>" button. While working the button reads "Checking prices…" and the list fades; no spinner chatter. Submits `POST /requests` with `{customer_id, text}` (or `reorder_of`) and polls `GET /requests/{id}` every 1.5 s.
3. `NEEDS_INPUT`: one card with the question in large text, large choice buttons and "Use default (…)" (`POST /requests/{id}/answer`).
4. Banner at the top of the list: "Delivered Fri 20 Nov · Order by Thu 19 Nov, 17:00" (quote `delivery_date`, `order_deadline`).
5. "We assumed …" lines from `intent.filled_gaps`, each with a "Change" link that puts the cursor back in the text field.
6. Left: **Your list** card. Each row: a soft coloured icon tile for the product category, product name, `quantity_text`, and in small text the line's delivery date and order deadline (or "No order deadline from Valio" / "Deadline passed"), unit price, line total. Substitutes and split lines say so in plain words. No quantity steppers (changing quantities needs a re-quote, not in the contract).
7. Playful customers only: **If you want** suggestions.
8. Right (stacks under on phones): summary card with Delivery, Order by, Products, VAT, big Total and the lime **Approve order** pill. Under it, small: "Prices and stock straight from Valio Aimo."
9. **The rules stay visible but friendly** (this is 20 % of the Valio score): with `NEEDS_APPROVAL`, a short amber note above the button lists each fired rule's `description` sentence. One tap approves the recommended quote (`POST /approvals/{id}/approve` with `quote_id`, `content_hash` and the customer's name as `approver`). If the status is already `ORDERED` (rules said `AUTO`), say "Ordered, it was within your rules." If `BLOCKED`, explain why and show no button.
8. After ordering: green card "Thanks Frank, order placed. Arrives Friday morning." plus a small "See order details" link to the decision log. Mark the order as simulated in the details, not in Frank's face.
9. Handle `409 QUOTE_CHANGED` ("Prices changed while you were looking. Here is the updated list.") and `410 QUOTE_EXPIRED` ("This list expired, checking prices again.").

### B. Approvals for someone else (`/approve`, `/approve/[id]`, phone first, 375 px)
Same visual style. Used when the buyer is not the one who signs (e.g. a cook orders, the owner approves). Inbox of pending approvals, then one card: what, total incl. VAT, delivery date, why approval is needed in plain words, options as selectable cards, big Approve / Reject. Same 409 / 410 handling. Approver name remembered in localStorage.

### C. Order details / decision log (`/audit/[requestId]`)
For judges and for anyone who asks "why". Calm vertical timeline of steps in plain language, a "Checked, nothing has been changed" badge from `chain_valid` (or "Warning: the record has been changed"), and "Copy as JSON".

### D. Footer
Small: "Prices and availability from the Valio Aimo test API. Orders are simulated. Ordering rules are fixed code, not AI."

## 4. Tech
- **Next.js (App Router) + TypeScript + Tailwind CSS** in `web/`. shadcn/ui is fine.
- **Types:** when `contract/openapi.json` exists, generate them with `openapi-typescript` into `web/lib/api-types.ts` (add an npm script `gen:types`). Until then, write `web/lib/types.ts` by hand from `docs/api_contract.md`, matching the fixtures exactly, and note it in the report.
- **API client** `web/lib/api.ts` with one function per endpoint.
- **Mock mode:** `NEXT_PUBLIC_USE_MOCK=true` serves responses built from `fixtures/api/` (copy or import them into `web/`; do not edit the originals). Simulate a run: steps appear one by one over about 6 seconds, then the request reaches its final state. An approval can be approved from `/approve`, and the request then shows the order. Include a way to trigger the 409 case.
- Otherwise calls `NEXT_PUBLIC_API_BASE` (e.g. `http://localhost:8000`).
- Responsive. Test the approval card at 375 px.
- Follow the design in section 3 exactly. Light mode only for the demo.

## Your task for this prompt
1. Read the three sources above and confirm in max 8 bullets what you will build.
2. Scaffold `web/` (Next.js + TS + Tailwind) with `web/.env.example` containing `NEXT_PUBLIC_API_BASE=http://localhost:8000` and `NEXT_PUBLIC_USE_MOCK=true`.
3. Build the types, `lib/api.ts`, the fixture-based mock, and the **Order** screen (`/`), working in mock mode end to end: type a request, see the list, swap a suggestion, place the order.
4. Then the decision log and the approvals screens.

**Report back:** the file tree of `web/`, how to run it, screenshots, and any contract changes you would propose.

## Contract changes to agree with Mikael
- Changing a quantity needs a re-quote (e.g. `POST /requests/{id}/requote` with `lines`). Not needed for the demo.
- A fixture for Frank's request after the answer (status `WAITING_APPROVAL` with a quote), so the mock does not have to invent it.
