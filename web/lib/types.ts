// Hand-written from docs/api_contract.md v0.3 and checked against fixtures/api.
// Replace with generated types (openapi-typescript on contract/openapi.json) when it covers v0.3.

export type RequestStatus =
  | "RUNNING"
  | "NEEDS_INPUT"
  | "WAITING_APPROVAL"
  | "ORDERED"
  | "BLOCKED"
  | "REJECTED"
  | "FAILED";

export type PolicyOutcome = "AUTO" | "NEEDS_APPROVAL" | "BLOCK";

export interface Quantity {
  value: number;
  unit: string;
}

export interface Offer {
  product_id: string;
  item_id: string;
  name: string;
  vendor: string | null;
  sales_unit: string;
  pack_description: string | null;
  quantity_sales_units: number;
  quantity_text: string;
  unit_price: number;
  line_total: number;
  line_total_with_tax: number;
  tax_rate: number;
  delivery_date: string | null;
  order_deadline: string | null;
  country_of_origin: string | null;
  labels: string[];
  tier?: "good" | "better" | "best" | null;
  allergens?: { contains: string[]; may_contain: string[]; data_available: boolean };
  dietary_check?: Record<string, "confirmed" | "violated" | "unknown">;
  is_split: boolean;
  is_substitute: boolean;
  substitute_source: "valio" | "agent" | null;
  flags: string[];
  reason: string | null;
}

export interface RuleFired {
  rule_id: string;
  description: string;
  outcome: PolicyOutcome;
  item_id?: string | null;
  product_id?: string | null;
}

export interface Quote {
  quote_id: string;
  request_id: string | null;
  status: string;
  option_label: string | null;
  delivery_date: string;
  order_deadline: string | null;
  lines: Offer[];
  total: number;
  total_with_tax: number;
  created_at: string;
  expires_at: string;
  content_hash: string;
  policy: { outcome: PolicyOutcome; rules_fired: RuleFired[] };
  agent_explanation: string | null;
}

export interface Order {
  order_id: string;
  quote_id: string;
  request_id: string | null;
  status: string;
  placed_at: string;
  approved_by: string | null;
  simulated: boolean;
  authority: string;
  delivery_date: string;
  order_deadline?: string | null;
  total: number;
  total_with_tax: number;
}

export interface FilledGap {
  field: string;
  value: unknown;
  source: "text" | "form" | "history" | "profile" | "default" | "answer";
  note: string;
}

export interface Question {
  question_id: string;
  field: string;
  text: string;
  choices: string[];
  default: string | null;
  asked_at: string;
}

export interface Suggestion {
  suggestion_id: string;
  kind: "seasonal" | "holiday" | "trend";
  title: string;
  reason: string;
  replaces_item_id: string | null;
  offer: Offer | null;
  status: "OFFERED" | "ACCEPTED" | "DECLINED";
}

export interface Step {
  seq: number;
  at: string;
  actor: string;
  type: string;
  title: string;
  detail: unknown;
  model: string | null;
  prev_hash: string | null;
  hash: string;
}

export interface LimosRequest {
  request_id: string;
  buyer_id: string;
  customer_id: string | null;
  preset_id: string | null;
  status: RequestStatus;
  created_at: string;
  input: { text: string | null; image_url: string | null };
  intent: {
    deliver_by?: string | null;
    assumptions?: string[];
    based_on?: { order_ref: string; title: string; date: string; guests_before: number | null; guests_now: number | null; scale: number } | null;
    filled_gaps?: FilledGap[];
  } | null;
  question: Question | null;
  steps: Step[];
  candidates?: Offer[];
  quote: Quote | null;
  suggestions: Suggestion[];
  approval_id: string | null;
  order: Order | null;
  error: { code: string; message: string } | null;
}

export interface PastOrder {
  order_ref: string;
  date: string;
  title: string;
  guests: number | null;
  total: number;
}

export interface CustomerSummary {
  customer_id: string;
  name: string;
  company: string;
  role: string;
  age_group: string;
  playful: boolean;
  simulated: boolean;
  last_order: { order_ref: string; date: string; title: string } | null;
}

export type PreferenceLevel = "require" | "prefer";
export interface Preference {
  key: string;
  level: PreferenceLevel;
}
export type Priority = "price" | "balanced" | "quality";

export interface Customer extends Omit<CustomerSummary, "last_order"> {
  buyer_id: string;
  preferences?: Preference[];
  exclude_allergens?: string[];
  defaults?: { priority: Priority; typical_guests: number | null; budget: number | null };
  order_history: PastOrder[];
}

export interface ApprovalSummary {
  approval_id: string;
  request_id: string | null;
  quote_id: string;
  buyer_id: string;
  created_at: string;
  summary: string;
  reasons: string[];
  expires_at: string;
  order_deadline: string | null;
}

export interface ApprovalDetail {
  approval_id: string;
  status: "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED";
  quote: Quote;
  request: { request_id: string; title: string; intent: LimosRequest["intent"] };
  options: Quote[];
  decided_by: string | null;
  decided_at: string | null;
  comment: string | null;
}

export interface Audit {
  request_id: string;
  chain_valid: boolean;
  steps: Step[];
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}
