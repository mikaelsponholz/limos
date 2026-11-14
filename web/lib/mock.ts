// Mock backend built from the shared contract fixtures (copied to lib/fixtures).
// Used when NEXT_PUBLIC_USE_MOCK=true. State lives in the browser tab only.
import approvalDetail from "./fixtures/approval-detail.json";
import approveResponse from "./fixtures/approve-response.json";
import audit from "./fixtures/audit.json";
import customer from "./fixtures/customer.json";
import customers from "./fixtures/customers.json";
import chefProfile from "./fixtures/customers/c_chef.json";
import requestChef from "./fixtures/request-chef.json";
import requestNeedsInput from "./fixtures/request-needs-input.json";
import requestWedding from "./fixtures/request.json";
import { day } from "./format";
import { PRIORITY_WORDS, allergenLabel, listWords, prefLabel } from "./settings";
import {
  ApiError,
  type ApprovalDetail,
  type ApprovalSummary,
  type Quote,
  type Audit,
  type Customer,
  type CustomerSummary,
  type LimosRequest,
  type Offer,
  type Order,
  type Preference,
  type Priority,
  type RuleFired,
  type Step,
} from "./types";

type Run = {
  final: LimosRequest;
  readyAt: number;
  options?: Quote[];
  title?: string;
  decision?: { status: "APPROVED" | "REJECTED"; by: string; at: string };
  overrides?: Overrides;
  startedAt?: number;
  asked?: { text: string; answer: string };
};
const runs = new Map<string, Run>();
let counter = 0;

const clone = <T>(x: unknown): T => structuredClone(x) as T;

// Seed: the wedding request from the fixtures waits for someone else's approval,
// with two options. Its expiry is moved to 30 minutes from now so the demo works any time.
{
  const final = clone<LimosRequest>(requestWedding);
  const detail = clone<ApprovalDetail>(approvalDetail);
  const expires = new Date(Date.now() + 30 * 60_000).toISOString();
  const options = detail.options.map((o) => ({ ...o, expires_at: expires }));
  final.quote = options[0];
  runs.set(final.request_id, { final, readyAt: 0, options, title: detail.request.title });
}

function runFor(approval_id: string): Run {
  const run = [...runs.values()].find((r) => r.final.approval_id === approval_id);
  if (!run) throw new ApiError(404, "NOT_FOUND", "Approval not found");
  return run;
}

function titleOf(run: Run): string {
  return run.title ?? run.final.intent?.based_on?.title ?? run.final.input.text ?? "Order";
}
const round2 = (n: number) => Math.round(n * 100) / 100;
const later = (ms: number) => Date.now() + ms;
// A real reorder takes 7 to 20 s; set NEXT_PUBLIC_MOCK_DELAY_MS=12000 to work on the loading state.
const MOCK_DELAY_MS = Number(process.env.NEXT_PUBLIC_MOCK_DELAY_MS) || 1800;
const fakeHash = () => "sha256:" + Math.random().toString(16).slice(2).padEnd(16, "0");

function newId(prefix: string) {
  counter += 1;
  return `${prefix}_${Date.now().toString(36)}${counter}`;
}

function retotal(req: LimosRequest) {
  const q = req.quote;
  if (!q) return;
  q.total = round2(q.lines.reduce((a, l) => a + l.line_total, 0));
  q.total_with_tax = round2(q.lines.reduce((a, l) => a + l.line_total_with_tax, 0));
  q.content_hash = fakeHash();
}

function guestsFrom(text: string): number | null {
  const m = text.match(/(\d{1,4})\s*(people|persons|guests|hengelle|henkeä|pax|for)?/i);
  return m ? Number(m[1]) : null;
}

// ---- Settings: the mock runs the same rules the gateway runs (docs/PLAN.md section 4) ----
type Overrides = { preferences?: Preference[]; exclude_allergens?: string[]; priority?: Priority; budget?: number | null };

const HARD_DIET = new Set(["lactose_free", "gluten_free", "vegetarian", "vegan"]);
const TIER: Record<Priority, "good" | "better" | "best"> = { price: "good", balanced: "better", quality: "best" };

function overridesFrom(body: Record<string, unknown>): Overrides {
  const o: Overrides = {};
  if (Array.isArray(body.preferences)) o.preferences = body.preferences as Preference[];
  if (Array.isArray(body.exclude_allergens)) o.exclude_allergens = body.exclude_allergens as string[];
  if (typeof body.priority === "string") o.priority = body.priority as Priority;
  if (typeof body.budget === "number") o.budget = body.budget;
  return o;
}

/** Applies the customer's settings to a finished request: picks the tier, re-runs the
 * allergen, diet and budget rules, and lists the settings used as a "We assumed" line. */
function applySettings(req: LimosRequest, o: Overrides = {}): LimosRequest {
  const q = req.quote;
  if (!q) return req;

  if (o.priority) {
    const want = TIER[o.priority];
    const swapped = new Set<string>();
    q.lines = q.lines.map((l) => {
      if (l.is_split || l.is_substitute || !l.tier) return l;
      const pool = (req.candidates ?? []).filter((x) => x.item_id === l.item_id && !x.is_split && x.tier);
      // "Lowest price" means the cheapest line, not the tier label (fixture tiers are not always cheapest).
      const c =
        o.priority === "price"
          ? pool.reduce<Offer | null>((best, x) => (!best || x.line_total < best.line_total ? x : best), null)
          : pool.find((x) => x.tier === want);
      if (!c || c.product_id === l.product_id || (o.priority === "price" && c.line_total >= l.line_total)) return l;
      swapped.add(l.item_id);
      return clone<Offer>(c);
    });
    // A swapped product covers the whole item, so the split line that topped up the old one goes.
    q.lines = q.lines.filter((l) => !(l.is_split && swapped.has(l.item_id)));
    q.policy = { ...q.policy, rules_fired: q.policy.rules_fired.filter((r) => !(r.rule_id === "SPLIT_DELIVERY" && r.item_id && swapped.has(r.item_id))) };
  }
  retotal(req);

  const rules: RuleFired[] = q.policy.rules_fired.filter(
    (r) =>
      !r.rule_id.startsWith("BUDGET_") &&
      !(o.exclude_allergens && r.rule_id.startsWith("ALLERGEN_")) &&
      !(o.preferences && (r.rule_id.startsWith("DIETARY_") || r.rule_id === "PREFERENCE_REQUIRED_UNMET")),
  );
  const add = (rule_id: string, outcome: RuleFired["outcome"], description: string, l?: Offer) =>
    rules.push({ rule_id, outcome, description, item_id: l?.item_id ?? null, product_id: l?.product_id ?? null });

  const excluded = o.exclude_allergens ?? [];
  if (excluded.length) {
    for (const l of q.lines) {
      const a = l.allergens ?? { contains: [], may_contain: [], data_available: false };
      const hit = excluded.filter((x) => a.contains.includes(x));
      const maybe = excluded.filter((x) => a.may_contain.includes(x) && !hit.includes(x));
      if (hit.length) add("ALLERGEN_CONTAINS", "BLOCK", `${l.name} contains ${listWords(hit.map(allergenLabel))}, which you never want.`, l);
      else if (maybe.length) add("ALLERGEN_MAY_CONTAIN", "NEEDS_APPROVAL", `${l.name} may contain ${listWords(maybe.map(allergenLabel))}.`, l);
      else if (!a.data_available)
        add(
          "ALLERGEN_DATA_MISSING",
          "NEEDS_APPROVAL",
          `${l.name} has no allergen information, so we can't confirm it is free of ${listWords(excluded.map(allergenLabel))}.`,
          l,
        );
    }
  }

  for (const p of (o.preferences ?? []).filter((x) => x.level === "require")) {
    for (const l of q.lines) {
      const v = l.dietary_check?.[p.key] ?? "unknown";
      // Same split as gateway/policy/engine.py: diet keys are hard, sourcing keys (finnish_origin...) need approval.
      if (!HARD_DIET.has(p.key)) {
        if (v !== "confirmed") add("PREFERENCE_REQUIRED_UNMET", "NEEDS_APPROVAL", `We can't confirm that ${l.name} is ${prefLabel(p.key)}.`, l);
      } else if (v === "violated") add("DIETARY_VIOLATED", "BLOCK", `${l.name} is not ${prefLabel(p.key)}, which you always want.`, l);
      else if (v === "unknown") add("DIETARY_UNKNOWN", "NEEDS_APPROVAL", `We can't confirm that ${l.name} is ${prefLabel(p.key)}.`, l);
    }
  }

  // Budget rules are recomputed only when the settings set a budget. Otherwise the fixture's
  // rules stay (request-chef.json says BUDGET_EXCEEDED at 24 EUR on a 20 EUR budget, although
  // PLAN section 4 would make that BUDGET_HARD_STOP), and BUDGET_EXCEEDED clears once the total fits.
  const budget = o.budget !== undefined ? o.budget : null;
  const fixtureBudget = (req.intent as { budget?: number | null } | null)?.budget ?? null;
  if (budget === null) {
    const kept = q.policy.rules_fired.filter((r) => r.rule_id.startsWith("BUDGET_"));
    for (const r of kept) if (!(r.rule_id === "BUDGET_EXCEEDED" && fixtureBudget !== null && q.total <= fixtureBudget)) rules.push(r);
  }
  if (budget !== null) {
    // The engine fires BUDGET_EXCEEDED on every overrun and adds BUDGET_HARD_STOP past 115 %.
    if (q.total > budget) add("BUDGET_EXCEEDED", "NEEDS_APPROVAL", `The ${q.total.toFixed(2)} EUR total exceeds the ${budget} EUR budget.`);
    if (q.total > budget * 1.15) add("BUDGET_HARD_STOP", "BLOCK", `The ${q.total.toFixed(2)} EUR total is far over the ${budget} EUR budget.`);
  }

  const blocked = rules.some((r) => r.outcome === "BLOCK");
  // Mock assumption kept from before: a customer always confirms with one tap, so no AUTO here.
  q.policy = { ...q.policy, outcome: blocked ? "BLOCK" : "NEEDS_APPROVAL", rules_fired: rules };
  q.status = blocked ? "BLOCKED" : "PENDING_APPROVAL";
  req.status = blocked ? "BLOCKED" : "WAITING_APPROVAL";
  req.approval_id = blocked ? null : (req.approval_id ?? `a_${req.request_id}`);

  const notes: string[] = [];
  if (o.priority) notes.push(PRIORITY_WORDS[o.priority]);
  const always = (o.preferences ?? []).filter((p) => p.level === "require").map((p) => prefLabel(p.key));
  const ifPossible = (o.preferences ?? []).filter((p) => p.level === "prefer").map((p) => prefLabel(p.key));
  if (always.length) notes.push(`always ${listWords(always)}`);
  if (ifPossible.length) notes.push(`${listWords(ifPossible)} if possible`);
  if (excluded.length) notes.push(`never ${listWords(excluded.map(allergenLabel))}`);
  if (o.budget !== undefined && o.budget !== null) notes.push(`budget ${o.budget} €`);
  if (notes.length && req.intent) {
    const gaps = (req.intent.filled_gaps ?? []).filter((g) => g.source !== "form");
    req.intent.filled_gaps = [...gaps, { field: "settings", value: o, source: "form", note: `Your settings: ${notes.join("; ")}.` }];
  }
  return req;
}

/** Pena's request after the gaps are filled. The fixtures stop at NEEDS_INPUT, so this
 * mock builds the quote from his last past order, scaled to the guest count. */
function penaQuoted(id: string, text: string, guests: number | null, answered: boolean, orderRef?: string): LimosRequest {
  const base = clone<LimosRequest>(requestWedding);
  const ni = clone<LimosRequest>(requestNeedsInput);
  type Past = Customer["order_history"][number] & { lines: { product_id: string; name: string; unit_price: number; quantity_text: string }[] };
  const history = (customer as unknown as { order_history: Past[] }).order_history;
  const past = history.find((h) => h.order_ref === orderRef) ?? history[0];
  const pl = past.lines[0];
  const portions = Number(pl.name.match(/(\d+) portions/)?.[1] ?? 24);
  guests = guests ?? past.guests ?? 30;
  const qty = Math.max(1, Math.ceil(guests / portions));
  const template = base.quote!.lines[0];
  const line: Offer = {
    ...template,
    product_id: pl.product_id,
    item_id: "it_cake",
    name: pl.name,
    vendor: "Example Bakery Oy",
    sales_unit: "ST",
    pack_description: `${portions} portions`,
    quantity_sales_units: qty,
    quantity_text: `${qty} x ${portions} portions`,
    unit_price: pl.unit_price,
    line_total: round2(qty * pl.unit_price),
    line_total_with_tax: round2(qty * pl.unit_price * 1.135),
    is_split: false,
    is_substitute: false,
    substitute_source: null,
    flags: [],
    tier: null,
    allergens: { contains: ["milk", "gluten", "egg"], may_contain: ["tree_nuts"], data_available: true },
    dietary_check: { lactose_free: "violated", gluten_free: "violated", finnish_origin: "confirmed", vegan: "violated" },
    reason: "Same cake as last time.",
  };
  const req: LimosRequest = {
    ...base,
    request_id: id,
    buyer_id: "ab-oy",
    customer_id: "c_pena",
    preset_id: null,
    status: "WAITING_APPROVAL",
    input: { text, image_url: null },
    intent: {
      ...base.intent,
      based_on: { order_ref: past.order_ref, title: past.title, date: past.date, guests_before: past.guests, guests_now: guests, scale: past.guests ? guests / past.guests : 1 },
      filled_gaps: [
        ...(ni.intent?.filled_gaps ?? [])
          .filter((g) => g.field !== "guests")
          .map((g) => (g.source === "history" ? { ...g, note: `Same as "${past.title}" on ${day(past.date)}.` } : g)),
        { field: "guests", value: guests, source: answered ? "answer" : "text", note: `For ${guests} people.` },
      ],
    },
    question: null,
    suggestions: [],
    approval_id: `a_${id}`,
    order: null,
    quote: {
      ...base.quote!,
      quote_id: `q_${id}`,
      request_id: id,
      option_label: null,
      lines: [line],
      // Mock assumption: a customer always confirms with one tap (see report to Mikael).
      policy: { outcome: "NEEDS_APPROVAL", rules_fired: [] },
      agent_explanation: `Same layer cake as on ${past.date}, scaled for ${guests} people.`,
    },
  };
  retotal(req);
  return req;
}

/** The chef's reorder, built from the seeded past order in customers/c_chef.json. */
function chefReorder(id: string, orderRef: string): LimosRequest {
  const base = clone<LimosRequest>(requestChef);
  type PastLine = { product_id: string; name: string; quantity_sales_units: number; sales_unit: string; quantity_text: string; unit_price: number; line_total: number };
  const history = (chefProfile as unknown as { order_history: (Customer["order_history"][number] & { lines: PastLine[] })[] }).order_history;
  const past = history.find((h) => h.order_ref === orderRef) ?? history[0];
  const template = base.quote!.lines[0];
  const lines: Offer[] = past.lines.map((pl, i) => ({
    ...template,
    product_id: pl.product_id,
    item_id: `it_${i + 1}`,
    name: pl.name,
    vendor: null,
    sales_unit: pl.sales_unit,
    pack_description: null,
    quantity_sales_units: pl.quantity_sales_units,
    quantity_text: pl.quantity_text,
    unit_price: pl.unit_price,
    line_total: pl.line_total,
    line_total_with_tax: round2(pl.line_total * 1.135),
    is_split: false,
    is_substitute: false,
    substitute_source: null,
    flags: [],
    tier: null,
    reason: "Same as last time.",
  }));
  const req: LimosRequest = {
    ...base,
    request_id: id,
    customer_id: "c_chef",
    input: { text: "Same as last time", image_url: null },
    intent: {
      ...base.intent,
      based_on: { order_ref: past.order_ref, title: past.title, date: past.date, guests_before: past.guests, guests_now: past.guests, scale: 1 },
      filled_gaps: [{ field: "items", value: past.order_ref, source: "history", note: `Same as "${past.title}" on ${day(past.date)}.` }],
    },
    suggestions: [],
    approval_id: null,
    order: null,
    quote: {
      ...base.quote!,
      quote_id: `q_${id}`,
      request_id: id,
      lines,
      policy: { outcome: "AUTO", rules_fired: [] },
      agent_explanation: `Same products as "${past.title}".`,
    },
  };
  retotal(req);
  return req;
}

// Decided with Mikael: Pena always confirms with one tap. The chef's restaurant runs on a
// mandate, so a list with no rules fired and under the auto limit is ordered straight away.
const AUTO_LIMIT = 1500;
function settleAuto(req: LimosRequest): LimosRequest {
  const q = req.quote;
  if (req.customer_id !== "c_chef" || !q || req.status !== "WAITING_APPROVAL") return req;
  if (q.policy.rules_fired.length || q.total > AUTO_LIMIT) return req;
  q.policy = { ...q.policy, outcome: "AUTO" };
  q.status = "AUTO_APPROVED";
  const order: Order = {
    ...clone<Order>(approveResponse.order),
    order_id: `o_${req.request_id}`,
    quote_id: q.quote_id,
    request_id: req.request_id,
    approved_by: null,
    placed_at: new Date().toISOString(),
    delivery_date: q.delivery_date,
    order_deadline: q.order_deadline,
    total: q.total,
    total_with_tax: q.total_with_tax,
    authority: `Within the restaurant's mandate: no rules fired and under ${AUTO_LIMIT} €.`,
  };
  return { ...req, status: "ORDERED", approval_id: null, order };
}

/** The decision log for this run, step by step, as the gateway would write it.
 * The wedding keeps the fixture log (it has the richest agent steps). Hashes are fake
 * but linked, so the page shows the chain as intact. */
function stepsFor(run: Run): Step[] {
  const r = run.final;
  const q = r.quote;
  const t0 = run.startedAt ?? Date.now() - 60_000;
  type Draft = { actor: string; type: string; title: string; detail?: unknown; model?: string | null; at?: number };
  let drafts: Draft[];

  if (!run.startedAt || /wedding/i.test(r.input.text ?? "")) {
    drafts = clone<Audit>(audit).steps.map((s, i) => ({ ...s, at: t0 + i * 400 }));
  } else {
    drafts = [];
    const based = r.intent?.based_on;
    drafts.push({
      actor: "customer",
      type: "REQUEST_RECEIVED",
      title: r.input.text && r.input.text !== "Same as last time" ? `Asked for: "${r.input.text}"` : `Asked for the same as "${based?.title ?? "last time"}"`,
    });
    drafts.push({ actor: "gateway", type: "CUSTOMER_LOADED", title: "Loaded the customer's profile and order history" });
    if (run.asked) {
      drafts.push({ actor: "agent", type: "QUESTION_ASKED", title: `Asked: ${run.asked.text}`, model: "gemini-flash" });
      drafts.push({ actor: "customer", type: "QUESTION_ANSWERED", title: `Answered: ${run.asked.answer}` });
    }
    for (const g of r.intent?.filled_gaps ?? []) {
      const yours = g.source === "form" || g.source === "answer";
      const title = g.source === "answer" ? `From your answer: ${g.note}` : g.source === "form" ? g.note : `Assumed: ${g.note}`;
      drafts.push({ actor: yours ? "customer" : "agent", type: "GAP_FILLED", title });
    }
    if (q) {
      drafts.push({ actor: "gateway", type: "TOOL_CALL", title: `Checked price and availability for ${q.lines.length} product${q.lines.length === 1 ? "" : "s"} for ${day(q.delivery_date)}` });
      for (const s of r.suggestions.filter((x) => x.status === "ACCEPTED")) drafts.push({ actor: "customer", type: "SUGGESTION_ACCEPTED", title: `Took the idea: ${s.title}` });
      drafts.push({ actor: "gateway", type: "QUOTE_CREATED", title: `Locked the prices: ${q.total.toFixed(2)} EUR excl. VAT, ${q.total_with_tax.toFixed(2)} EUR with VAT` });
      const rules = q.policy.rules_fired;
      const outcome =
        q.policy.outcome === "BLOCK"
          ? `Can't be ordered: ${rules.filter((x) => x.outcome === "BLOCK").map((x) => x.description).join(" ")}`
          : q.policy.outcome === "AUTO"
            ? "Can be ordered without a check: no rule fired and the total is within the limits"
            : rules.length
              ? `Needs a check: ${rules.map((x) => x.description).join(" ")}`
              : "Nothing unusual. The customer confirms the list with one tap";
      drafts.push({ actor: "policy", type: "POLICY_DECISION", title: outcome, detail: { outcome: q.policy.outcome, rule_ids: rules.map((x) => x.rule_id) } });
      if (r.approval_id && q.policy.outcome !== "BLOCK") drafts.push({ actor: "gateway", type: "APPROVAL_REQUESTED", title: "Waiting for the customer to approve the list" });
    }
    drafts = drafts.map((d, i) => ({ ...d, at: t0 + i * 300 }));
  }

  if (run.decision)
    drafts.push({
      actor: "human",
      type: run.decision.status === "APPROVED" ? "APPROVED" : "REJECTED",
      title: `${run.decision.by} ${run.decision.status === "APPROVED" ? "approved this exact list and its prices" : "said no to this list"}`,
      at: Date.parse(run.decision.at),
    });
  if (r.order)
    drafts.push({
      actor: "gateway",
      type: "ORDER_PLACED",
      title: `Order placed with Valio Aimo, arrives ${day(r.order.delivery_date)}${r.order.approved_by ? "" : " (ordered automatically)"}`,
      at: Date.parse(r.order.placed_at),
    });

  let prev: string | null = null;
  return drafts.map((d, i) => {
    const hash = `sha256:${(i + 1).toString(16).padStart(4, "0")}${r.request_id.slice(-8)}`;
    const step: Step = {
      seq: i + 1,
      at: new Date(d.at ?? t0).toISOString(),
      actor: d.actor,
      type: d.type,
      title: d.title,
      detail: d.detail ?? {},
      model: d.model ?? null,
      prev_hash: prev,
      hash,
    };
    prev = hash;
    return step;
  });
}

export const mock = {
  async customers(): Promise<CustomerSummary[]> {
    return clone(customers);
  },

  async customer(id: string): Promise<Customer> {
    if (id === "c_pena") return clone(customer);
    if (id === "c_chef") return clone(chefProfile);
    const s = (customers as unknown as CustomerSummary[]).find((c) => c.customer_id === id);
    if (!s) throw new ApiError(404, "NOT_FOUND", "Customer not found");
    return { ...clone<CustomerSummary>(s), buyer_id: "demo-catering", order_history: s.last_order ? [{ ...s.last_order, guests: null, total: 0 }] : [] };
  },

  async createRequest(body: { customer_id: string; text?: string; reorder_of?: string } & Record<string, unknown>) {
    const id = newId("r");
    const text = body.text ?? "";
    let final: LimosRequest;
    if (/wedding/i.test(text)) {
      final = { ...clone<LimosRequest>(requestWedding), request_id: id, customer_id: body.customer_id, approval_id: `a_${id}` };
      final.input.text = text;
      if (final.quote) final.quote.quote_id = `q_${id}`;
    } else if (body.customer_id === "c_pena") {
      const guests = guestsFrom(text);
      if (guests || body.reorder_of) {
        final = penaQuoted(id, text || "Same as last time", guests, false, body.reorder_of);
      } else {
        final = { ...clone<LimosRequest>(requestNeedsInput), request_id: id };
        final.input.text = text;
      }
    } else if (body.customer_id === "c_chef" && body.reorder_of) {
      final = chefReorder(id, body.reorder_of);
    } else {
      final = { ...clone<LimosRequest>(requestChef), request_id: id, customer_id: body.customer_id };
      final.input.text = text || final.input.text;
      final.approval_id = `a_${id}`;
    }
    if (final.quote) final.quote.request_id = id;
    const overrides = overridesFrom(body);
    runs.set(id, { final: settleAuto(applySettings(final, overrides)), readyAt: later(MOCK_DELAY_MS), overrides, startedAt: Date.now() });
    return { request_id: id, status: "RUNNING" as const };
  },

  async getRequest(id: string): Promise<LimosRequest> {
    const run = runs.get(id);
    if (!run) throw new ApiError(404, "NOT_FOUND", "Request not found");
    // No steps while running, so the progress card moves on time alone, as if Valio were slow.
    if (Date.now() < run.readyAt) return { ...clone<LimosRequest>(run.final), status: "RUNNING", quote: null, question: null, suggestions: [], steps: [] };
    return clone(run.final);
  },

  async answer(id: string, question_id: string, answer: string | null) {
    const run = runs.get(id);
    if (!run || run.final.status !== "NEEDS_INPUT" || run.final.question?.question_id !== question_id)
      throw new ApiError(409, "NO_OPEN_QUESTION", "There is no open question.");
    const guests = Number(answer ?? run.final.question.default ?? 30) || 30;
    runs.set(id, {
      ...run,
      final: applySettings(penaQuoted(id, run.final.input.text ?? "", guests, true), run.overrides),
      readyAt: later(1500),
      asked: { text: run.final.question.text, answer: answer === null ? `${guests} (the default)` : String(guests) },
    });
    return { request_id: id, status: "RUNNING" as const };
  },

  async acceptSuggestion(id: string, suggestion_id: string) {
    const run = runs.get(id);
    const s = run?.final.suggestions.find((x) => x.suggestion_id === suggestion_id);
    if (!run || !s || s.status !== "OFFERED") throw new ApiError(409, "SUGGESTION_NOT_OFFERED", "This suggestion is no longer open.");
    const req = clone<LimosRequest>(run.final);
    const q = req.quote!;
    if (s.offer) {
      q.lines = s.replaces_item_id ? q.lines.map((l) => (l.item_id === s.replaces_item_id ? s.offer! : l)) : [...q.lines, s.offer];
    }
    req.suggestions = req.suggestions.map((x) => (x.suggestion_id === suggestion_id ? { ...x, status: "ACCEPTED" } : x));
    q.quote_id = `${q.quote_id}_s`;
    // The rules run again on the new quote (applySettings re-totals and re-checks the budget).
    runs.set(id, { ...run, final: applySettings(req, run.overrides), readyAt: later(1200) });
    return { request_id: id, status: "RUNNING" as const };
  },

  async approvals(): Promise<ApprovalSummary[]> {
    return [...runs.values()]
      .filter((r) => r.final.status === "WAITING_APPROVAL" && r.final.approval_id && Date.now() >= r.readyAt)
      .map((r) => {
        const q = r.final.quote!;
        return {
          approval_id: r.final.approval_id!,
          request_id: r.final.request_id,
          quote_id: q.quote_id,
          buyer_id: r.final.buyer_id,
          created_at: q.created_at,
          summary: `${titleOf(r)} · ${q.total_with_tax.toFixed(2)} € incl. VAT`,
          reasons: q.policy.rules_fired.map((x) => x.rule_id),
          expires_at: q.expires_at,
          order_deadline: q.order_deadline,
        };
      })
      .reverse();
  },

  async approval(approval_id: string): Promise<ApprovalDetail> {
    const run = runFor(approval_id);
    const q = run.final.quote!;
    const options = run.options ?? [q];
    return clone({
      approval_id,
      status: run.decision?.status ?? "PENDING",
      quote: q,
      request: { request_id: run.final.request_id, title: titleOf(run), intent: run.final.intent },
      options: options.length > 1 ? options : [],
      decided_by: run.decision?.by ?? null,
      decided_at: run.decision?.at ?? null,
      comment: null,
    });
  },

  async reject(approval_id: string, body: { approver: string; reason?: string }) {
    const run = runFor(approval_id);
    if (run.decision) throw new ApiError(409, "ALREADY_DECIDED", "This order was already decided.");
    run.decision = { status: "REJECTED", by: body.approver, at: new Date().toISOString() };
    run.final = { ...run.final, status: "REJECTED" };
    return { status: "REJECTED" as const };
  },

  /** Demo helper: the prices change after the approver opened the card, so approving gives 409. */
  async simulatePriceChange(approval_id: string) {
    const run = runFor(approval_id);
    const q = run.final.quote!;
    const fresh = { ...q, content_hash: fakeHash() };
    run.final = { ...run.final, quote: fresh };
    if (run.options) run.options = run.options.map((o) => (o.quote_id === q.quote_id ? fresh : o));
  },

  async approve(approval_id: string, body: { quote_id: string; content_hash: string; approver: string }) {
    const run = runFor(approval_id);
    if (run.decision || run.final.status !== "WAITING_APPROVAL") throw new ApiError(409, "ALREADY_DECIDED", "This order was already decided.");
    const q = (run.options ?? [run.final.quote!]).find((o) => o.quote_id === body.quote_id);
    if (!q || q.content_hash !== body.content_hash)
      throw new ApiError(409, "QUOTE_CHANGED", "The prices changed after you opened the list.");
    run.decision = { status: "APPROVED", by: body.approver, at: new Date().toISOString() };
    run.final = { ...run.final, quote: q };
    const order: Order = {
      ...clone<Order>(approveResponse.order),
      order_id: `o_${run.final.request_id}`,
      quote_id: q.quote_id,
      request_id: run.final.request_id,
      approved_by: body.approver,
      placed_at: new Date().toISOString(),
      delivery_date: q.delivery_date,
      order_deadline: q.order_deadline,
      total: q.total,
      total_with_tax: q.total_with_tax,
      authority: `${body.approver} approved this exact list and its prices.`,
    };
    run.final = { ...run.final, status: "ORDERED", order };
    return { status: "APPROVED" as const, order };
  },

  async audit(id: string): Promise<Audit> {
    const run = runs.get(id);
    if (!run) throw new ApiError(404, "NOT_FOUND", "Request not found");
    return { request_id: id, chain_valid: true, steps: stepsFor(run) };
  },
};
