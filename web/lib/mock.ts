// Mock backend built from the shared contract fixtures (copied to lib/fixtures).
// Used when NEXT_PUBLIC_USE_MOCK=true. State lives in the browser tab only.
import approvalDetail from "./fixtures/approval-detail.json";
import approveResponse from "./fixtures/approve-response.json";
import audit from "./fixtures/audit.json";
import customer from "./fixtures/customer.json";
import customers from "./fixtures/customers.json";
import requestChef from "./fixtures/request-chef.json";
import requestNeedsInput from "./fixtures/request-needs-input.json";
import requestWedding from "./fixtures/request.json";
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
} from "./types";

type Run = {
  final: LimosRequest;
  readyAt: number;
  options?: Quote[];
  title?: string;
  decision?: { status: "APPROVED" | "REJECTED"; by: string; at: string };
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

/** Pena's request after the gaps are filled. The fixtures stop at NEEDS_INPUT, so this
 * mock builds the quote from his last past order, scaled to the guest count. */
function penaQuoted(id: string, text: string, guests: number, answered: boolean): LimosRequest {
  const base = clone<LimosRequest>(requestWedding);
  const ni = clone<LimosRequest>(requestNeedsInput);
  const past = (customer as unknown as Customer & { order_history: { lines: { product_id: string; name: string; unit_price: number }[] }[] }).order_history[0];
  const pl = past.lines[0];
  const qty = Math.max(1, Math.ceil(guests / 24));
  const template = base.quote!.lines[0];
  const line: Offer = {
    ...template,
    product_id: pl.product_id,
    item_id: "it_cake",
    name: pl.name,
    vendor: "Example Bakery Oy",
    sales_unit: "ST",
    pack_description: "24 portions",
    quantity_sales_units: qty,
    quantity_text: `${qty} x 24 portions`,
    unit_price: pl.unit_price,
    line_total: round2(qty * pl.unit_price),
    line_total_with_tax: round2(qty * pl.unit_price * 1.135),
    is_split: false,
    is_substitute: false,
    substitute_source: null,
    flags: [],
    reason: "Same cake as your last order.",
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
        ...(ni.intent?.filled_gaps ?? []).filter((g) => g.field !== "guests"),
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

export const mock = {
  async customers(): Promise<CustomerSummary[]> {
    return clone(customers);
  },

  async customer(id: string): Promise<Customer> {
    if (id === "c_pena") return clone(customer);
    const s = (customers as unknown as CustomerSummary[]).find((c) => c.customer_id === id);
    if (!s) throw new ApiError(404, "NOT_FOUND", "Customer not found");
    return { ...clone<CustomerSummary>(s), buyer_id: "demo-catering", order_history: s.last_order ? [{ ...s.last_order, guests: null, total: 0 }] : [] };
  },

  async createRequest(body: { customer_id: string; text?: string; reorder_of?: string }) {
    const id = newId("r");
    const text = body.text ?? "";
    let final: LimosRequest;
    if (/wedding/i.test(text)) {
      final = { ...clone<LimosRequest>(requestWedding), request_id: id, customer_id: body.customer_id };
      final.input.text = text;
    } else if (body.customer_id === "c_pena") {
      const guests = guestsFrom(text);
      if (guests || body.reorder_of) {
        final = penaQuoted(id, text || "Same as last time", guests ?? 30, false);
      } else {
        final = { ...clone<LimosRequest>(requestNeedsInput), request_id: id };
        final.input.text = text;
      }
    } else {
      final = { ...clone<LimosRequest>(requestChef), request_id: id, customer_id: body.customer_id };
      final.input.text = text || final.input.text;
      final.approval_id = `a_${id}`;
    }
    if (final.quote) final.quote.request_id = id;
    runs.set(id, { final, readyAt: later(1800) });
    return { request_id: id, status: "RUNNING" as const };
  },

  async getRequest(id: string): Promise<LimosRequest> {
    const run = runs.get(id);
    if (!run) throw new ApiError(404, "NOT_FOUND", "Request not found");
    if (Date.now() < run.readyAt) return { ...clone<LimosRequest>(run.final), status: "RUNNING", quote: null, question: null, suggestions: [] };
    return clone(run.final);
  },

  async answer(id: string, question_id: string, answer: string | null) {
    const run = runs.get(id);
    if (!run || run.final.status !== "NEEDS_INPUT" || run.final.question?.question_id !== question_id)
      throw new ApiError(409, "NO_OPEN_QUESTION", "There is no open question.");
    const guests = Number(answer ?? run.final.question.default ?? 30) || 30;
    runs.set(id, { final: penaQuoted(id, run.final.input.text ?? "", guests, true), readyAt: later(1500) });
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
    retotal(req);
    // The rules run again on the new quote. The fixture budget rule no longer fires if under budget.
    if (q.policy.rules_fired.some((r) => r.rule_id === "BUDGET_EXCEEDED") && q.total <= 20) q.policy.rules_fired = q.policy.rules_fired.filter((r) => r.rule_id !== "BUDGET_EXCEEDED");
    runs.set(id, { final: req, readyAt: later(1200) });
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
    return { ...clone<Audit>(audit), request_id: id };
  },
};
