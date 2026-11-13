# WEB PROMPT 0: Limos web app

You are the frontend builder for **Limos**, our Junction 2026 hackathon project. Read this whole prompt, then `prompts/00_master_context.md` (the full project context) and `docs/api_contract.md` (the API contract with the backend). The backend is built by a teammate in parallel, so you build against **mock data first** and switch to the real backend later with one env variable.

Work only in the `web/` folder. Do not touch backend code.

---

## 1. What Limos is (short)

An AI purchasing agent buys food from the wholesaler Valio Aimo on behalf of a professional kitchen (e.g. a hotel). Pitch: **"The AI proposes, the rules decide, the human signs."**

- The **Gemini agent** understands the request, searches products, picks the best option and explains why.
- The **gateway** locks the price into a quote and runs deterministic **policy rules**: `AUTO`, `NEEDS_APPROVAL` or `BLOCK`.
- A **human** approves when the rules say so, ideally from their phone.
- Everything is written to a **decision log**.

The web app is what the judges see. 30% of the score is a working end-to-end demo and 20% is visible guardrails, so the UI must make the flow and the controls obvious at a glance.

## 2. Users and screens

### A. Kitchen buyer (desktop or tablet)
1. **New request** (`/`):
   - Text field ("What do you need?"), delivery date picker, optional image upload (photo of a handwritten order list).
   - Three demo shortcut buttons that prefill: "Routine order" (small, auto-approved), "1,000 L lactose-free milk by Monday" (needs approval), "Partial stock" (exception).
   - Buyer selector (e.g. "Hotel Helsinki", default).
2. **Request in progress** (`/requests/[id]`):
   - **Live timeline** of `steps` (poll `GET /requests/{id}` every 1.5 s while `RUNNING`). Each step shows actor icon (user, agent, gateway, policy, human), title, time. Expandable detail.
   - **Parsed intent** card: what the agent understood (item, quantity, unit, constraints, deliver by).
   - **Candidates** table: name, pack, price per L/kg, line total, available date, reliability, CO2, dietary check, flags. The chosen one highlighted; substitutes labelled "Valio-approved substitute" or "AI-proposed substitute".
   - **Quote + policy card**: total excl./incl. VAT, expiry countdown, and the **policy outcome as a big badge** (green AUTO, amber NEEDS APPROVAL, red BLOCKED) with the rules that fired in plain language.
   - **Agent explanation** (quote `agent_explanation`).
   - Final state: order confirmed / waiting for approval / blocked / rejected.

### B. Approver (phone first)
3. **Approvals inbox** (`/approve`): list of pending approvals, newest first, with summary, reasons and expiry.
4. **Approval card** (`/approve/[id]`), designed for a phone screen (375 px wide):
   - What is being ordered, total incl. VAT, delivery date.
   - **Why approval is needed**: the fired rules in plain language.
   - Agent's explanation.
   - If `options` has several quotes (exception scenario): show them as selectable cards (e.g. "Split: 700 L Mon + 300 L Tue" vs "Valio-approved substitute, all on Mon").
   - Big **Approve** and **Reject** buttons. Approve sends `quote_id` and `content_hash`. Handle `409` ("This quote changed after you opened it. Reload.").
   - Approver name field, remembered in localStorage.

### C. Decision log
5. **Audit view** (`/audit/[requestId]`): the full append-only log as a clean vertical timeline, readable by a non-technical person. A "Copy as JSON" button. This is the "explain decisions" bonus and the trust story.

### D. Small extras
- Header with "Limos", nav (New request, Approvals with a pending count badge, link "Open spec" to the backend's `/openapi.json`).
- Footer note: "Prices and availability from Valio Aimo mock APIs. Policy decisions are deterministic code, not AI."

## 3. Tech
- **Next.js (App Router) + TypeScript + Tailwind CSS.** shadcn/ui is fine for components.
- Data fetching: plain `fetch` + a small typed API client in `web/lib/api.ts`. Polling with a simple hook (SWR or React Query is fine).
- Types in `web/lib/types.ts`, matching `docs/api_contract.md` exactly.
- **Mock mode:** if `NEXT_PUBLIC_USE_MOCK=true`, the API client uses an in-memory mock (`web/lib/mock.ts`) that simulates the three demo scenarios, including steps appearing one by one over about 6 seconds, a pending approval that can be approved from `/approve`, and a 409 case. Otherwise it calls `NEXT_PUBLIC_API_BASE`.
- Use realistic mock data from the real Valio products, e.g. `464306` "Valio Arki Eila kevytmaitojuoma 1 l laktoositon" (1.01 EUR/L, list 1.44, Stock, case of 20, Valio substitutes incl. `465039`), `465039` "Valio Eila kevytmaitojuoma 1 l laktoositon" (KI of 5, Terminal), `463038` "Valio Eila kevytmaitojuoma 10 l novobox laktoositon" (KI of 2).
- Responsive. The approval card must look great on a phone. Test at 375 px.
- Clean, calm, trustworthy look: white background, one accent colour, clear status colours (green / amber / red), large readable numbers. No dashboards full of noise.
- English UI text.

## 4. Rules
1. Only edit files under `web/` (plus `web/.env.example`). Never commit `.env*` files with values.
2. Types follow `docs/api_contract.md`. If you think the contract needs a change, do not change it silently: list the proposed change in your report.
3. No AI calls from the frontend. The frontend only talks to the backend (or the mock).
4. Every screen must handle loading, empty and error states.
5. Keep it demo-first: the three scenarios must run smoothly in mock mode before anything fancy.

## Your task for this prompt
1. Read the two documents mentioned at the top and confirm in max 8 bullets what you will build.
2. Scaffold `web/` (Next.js + TS + Tailwind), with `web/.env.example` containing `NEXT_PUBLIC_API_BASE=http://localhost:8000` and `NEXT_PUBLIC_USE_MOCK=true`.
3. Build `lib/types.ts`, `lib/api.ts`, `lib/mock.ts` (all three scenarios) and the screens **New request** and **Request in progress** working in mock mode.
4. Stop there. Approvals and audit come in the next prompt.

**Report back:** the file tree of `web/`, how to run it, screenshots or a description of the two screens for the milk scenario, and any proposed contract changes.
