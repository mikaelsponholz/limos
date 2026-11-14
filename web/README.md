# Limos web

Order screen for Valio Aimo customers: write what you need, get a shopping list with delivery date and order deadline, approve with one tap.

```bash
cd web
cp .env.example .env.local   # NEXT_PUBLIC_USE_MOCK=true runs on fixtures, no backend needed
npm install
npm run dev                  # copies ../fixtures/api into lib/fixtures first
```

Set `NEXT_PUBLIC_USE_MOCK=false` and `NEXT_PUBLIC_API_BASE=http://localhost:8000` to use the real backend.

Mock flows: pick Pena in the header and write a request without a number (asks how many people) or with one; pick the chef for a reorder with a seasonal suggestion. Pena's quoted list is built by the mock from his order history, because the fixtures stop at the question.

Screens: `/` (order), `/audit/[requestId]` (order details and decision log). Approvals for someone else come next.
