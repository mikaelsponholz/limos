# Limos UI contract

The JSON shapes the API returns and the UI consumes. The source of truth is [`types/contract.ts`](../types/contract.ts). Mock data for every shape is in [`fixtures/contract/`](../fixtures/contract/), so the UI can be built before the agent works.

Changing a shape means updating `types/contract.ts`, this file, `lib/contract/schemas.ts` and the fixtures in the same change.

## Conventions

- Money is `{ amount, currency: "EUR" }`, two decimals, **net (VAT 0 %)** unless the field says `gross`. B2B kitchens compare net prices.
- Quantities are `{ value, unit }` with `g`, `kg`, `ml`, `l` or `pcs`.
- Dates are `YYYY-MM-DD`. Timestamps are ISO 8601 with offset (Helsinki time).
- `null` means known to be missing. An absent optional field means not provided.
- Every product in the fixtures is invented (ids `9000xx`, "Example" brands). Real Valio data is never committed.
- Text the buyer reads (`reason`, `summary`, `effect`, `message`) is ready to show as is. The UI should not rebuild it.

## Endpoints

| Method and path | Body | Returns | Fixture |
|---|---|---|---|
| `POST /api/runs` | `EventRequest` | `CreateRunResponse` | `event-request.json`, `create-run-response.json` |
| `GET /api/runs/:id` | | `RunSnapshot` | `run-snapshot.json` |
| `POST /api/runs/:id/approvals/:approvalId` | `ApprovalDecision` | `RunSnapshot` | `approval-decision.json` |
| `GET /api/runs/:id/audit?format=json` | | `AuditEntry[]` | `audit-log.json` |
| `GET /api/runs/:id/audit?format=md` | | Markdown text | |

The run is asynchronous. Poll `GET /api/runs/:id` about once a second while `status` is `planning`, `sourcing` or `ordering`. Stop polling at `awaiting_approval`, `ordered` or `failed`. New audit entries appear as the agent works, so the trail can be shown live.

Errors: `400` with `{ error, details }` for an invalid body, `404` for an unknown run or approval, `409` when an approval is already resolved.

## Shapes

### EventRequest (UI → API)

What the buyer asks for. `description` is free text. The agent fills only fields the buyer left blank, and fields the buyer set always win.

| Field | Notes |
|---|---|
| `description` | Free text as written |
| `eventDate`, `deliveryDate` | `deliveryDate` is the latest acceptable delivery. Defaults to the day before the event |
| `guests` | Default servings for every dish |
| `dishes[]` | `{ name, servings?, notes? }`. Can be empty when the description lists dishes |
| `preferences[]` | `{ key, level }`. Keys: `finnish_origin`, `organic`, `lactose_free`, `gluten_free`, `vegetarian`, `vegan`, `low_carbon`. `require` = a human approves if it can't be met. `prefer` = ranks higher |
| `excludeAllergens[]` | `milk`, `lactose`, `gluten`, `wheat`, `egg`, `fish`, `crustaceans`, `molluscs`, `peanuts`, `tree_nuts`, `soy`, `sesame`, `celery`, `mustard`, `sulphites`, `lupin` |
| `budget` | Net, whole order. Optional |
| `priority` | `price`, `balanced`, `quality`. Picks the default tier |
| `buyer`, `locale` | `locale` is `fi` or `en` |

### IngredientPlan

`dishes[].ingredients[]` show the recipe view: per-serving and total quantity, and which preferences apply. `aggregated[]` merges the same ingredient across dishes; each aggregated ingredient becomes one cart line. `assumptions[]` are things the agent assumed and should be shown to the buyer.

### CartLine

One ingredient to buy.

| Field | Notes |
|---|---|
| `needed` | Total quantity the plan needs |
| `chosen` | The `ProductOption` picked. `null` when nothing orderable was found |
| `alternatives[]` | Other tiers and fallbacks, best first. The UI shows good / better / best from `chosen` + `alternatives` by `tier` |
| `split` | A second product when stock is short. `null` otherwise |
| `availability` | `status`: `available`, `partial`, `late`, `unavailable`, `unknown`. `availableAmount` is in sales units, `null` when the supplier doesn't report it |
| `exceptions[]` | `{ kind, severity, message }`. Severity `info`, `warning`, `blocking` |
| `substitution` | Set when the chosen product replaces another. `source` is `valio_substitution_list` or `agent_search` |
| `decision` | `status`: `auto_approved`, `needs_approval`, `approved`, `rejected`, `blocked`, `ordered`, plus `ruleIds` (see `docs/POLICY.md`) and a plain `reason` |

`ProductOption` holds the product, `salesUnits` to order, what they `covers`, `lineTotal`, `score` (0 to 100), `matched` and `missed` preferences, and a code-written `reason`.

`ProductSummary` holds names, origin, `labels[]`, allergens (`contains`, `mayContain`, `dataAvailable`), `packSize`, sales unit code and label, net and gross unit price, VAT rate, comparison price per kg or l, delivery reliability (0 to 1), kg CO2e per kg and logistic model. Missing data is `null`, never a guess.

Label meanings: `finnish_label` = a Finnish origin label on the package (Blue Swan, key flag, Finnish heart). `finnish_origin` = country of origin is Finland. `organic_label` = certified organic label. `organic_name` = only the name says organic ("luomu"), weaker evidence.

### Cart

`lines[]`, `deliveryDate`, `totals` (net, gross, estimated `co2eKg` with the count of lines that had carbon data), and `budget` (`within`, `over`, `blocked`). `null` when no budget was given.

### ApprovalItem

Something the rules sent to a human. `scope` is `line` or `cart`. `reason` says which rule escalated it and why. `options[]` are the choices, each with a one-sentence `effect` and an optional `ProductOption`. `recommendedOptionId` is the agent's pick. Show it as the primary choice, never as a decision already taken. `resolution` is filled once resolved.

### ApprovalDecision (UI → API)

`{ decision: "approve" | "reject", optionId?, approver, note? }`. `optionId` is required to approve an item with more than one option. After the last pending item is resolved, the agent re-runs the rules, re-checks availability and places the order.

### OrderResult

`status`: `placed`, `partially_placed`, `not_placed`. `simulated: true` while orders go to our simulated order service (the Valio mock API has no order endpoint). Show this plainly in the UI. `authority` explains in one sentence why the agent was allowed to place the order. `excludedLines[]` lists rejected lines with the reason.

### AuditEntry

| Field | Notes |
|---|---|
| `seq`, `at` | Order and time |
| `actor` | `{ type: agent \| rules \| human \| system, name, model? }`. `model` is set for LLM steps |
| `action` | `request_received`, `plan_created`, `products_searched`, `candidates_filtered`, `price_checked`, `availability_checked`, `product_selected`, `exception_detected`, `substitution_proposed`, `rule_applied`, `approval_requested`, `approval_resolved`, `availability_rechecked`, `order_placed`, `error` |
| `summary` | One plain sentence. This is what the trail shows |
| `lineId`, `productIds`, `ruleIds`, `data` | Optional links and details |
| `prevHash`, `hash` | SHA-256 chain. `hash` covers the entry including `prevHash`, so editing any entry breaks the chain |

### PolicySummary

The agent's authority: auto-order limit, per-line limit, budget hard stop, substitution limits, overbuy limit and the may-contain policy. Show it near the cart so the buyer knows what the agent may do alone.

### RunSnapshot

Everything for one run: `status`, `request`, `plan`, `cart`, `approvals`, `order`, `policy`, `audit`, `error`. Fields that aren't ready yet are `null` (or `[]` for lists).

## Fixture scenario

All fixtures describe one run: a wedding for 120 guests on 21 November 2026, with lactose-free required, Finnish and organic preferred, tree nuts excluded, and a €900 budget.

| Line | What it shows |
|---|---|
| Butter | Auto-approved, three tiers (good / better / best) |
| Whipping cream | Partial stock, split across two products, needs approval (D3) |
| Rye bread | May contain tree nuts, needs approval (A2), safer alternative offered |
| Cucumber | Late delivery, replaced from Valio's substitution list, auto-approved (X1) |

`run-snapshot.json` is the state at `awaiting_approval`. `order-result.json` is the result after both approvals (cream split approved, bread switched to the safer option).
