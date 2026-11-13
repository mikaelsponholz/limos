// One function per backend endpoint (docs/api_contract.md v0.3).
// NEXT_PUBLIC_USE_MOCK=true serves fixtures instead of calling the backend.
import { mock } from "./mock";
import { ApiError, type ApprovalDetail, type ApprovalSummary, type Audit, type Customer, type CustomerSummary, type LimosRequest, type Order } from "./types";

export const USE_MOCK = process.env.NEXT_PUBLIC_USE_MOCK !== "false";
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let code = "HTTP_" + res.status;
    let message = res.statusText;
    try {
      const body = await res.json();
      code = body?.error?.code ?? code;
      message = body?.error?.message ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, code, message);
  }
  return res.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  call<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

type Started = { request_id: string; status: "RUNNING" };

export const api = {
  customers: (): Promise<CustomerSummary[]> => (USE_MOCK ? mock.customers() : call("/customers")),
  customer: (id: string): Promise<Customer> => (USE_MOCK ? mock.customer(id) : call(`/customers/${id}`)),

  createRequest: (body: {
    customer_id: string;
    text?: string;
    reorder_of?: string;
    preferences?: unknown;
    exclude_allergens?: unknown;
    priority?: unknown;
    budget?: unknown;
  }): Promise<Started> =>
    USE_MOCK ? mock.createRequest(body) : post("/requests", body),
  getRequest: (id: string): Promise<LimosRequest> => (USE_MOCK ? mock.getRequest(id) : call(`/requests/${id}`)),
  answer: (id: string, question_id: string, answer: string | null): Promise<Started> =>
    USE_MOCK ? mock.answer(id, question_id, answer) : post(`/requests/${id}/answer`, { question_id, answer }),
  acceptSuggestion: (id: string, suggestion_id: string): Promise<Started> =>
    USE_MOCK ? mock.acceptSuggestion(id, suggestion_id) : post(`/requests/${id}/suggestions/${suggestion_id}/accept`),

  approve: (
    approval_id: string,
    body: { quote_id: string; content_hash: string; approver: string },
  ): Promise<{ status: "APPROVED"; order: Order }> =>
    USE_MOCK ? mock.approve(approval_id, body) : post(`/approvals/${approval_id}/approve`, body),

  approvals: (): Promise<ApprovalSummary[]> => (USE_MOCK ? mock.approvals() : call("/approvals?status=PENDING")),
  approval: (id: string): Promise<ApprovalDetail> => (USE_MOCK ? mock.approval(id) : call(`/approvals/${id}`)),
  reject: (id: string, body: { approver: string; reason?: string }): Promise<{ status: "REJECTED" }> =>
    USE_MOCK ? mock.reject(id, body) : post(`/approvals/${id}/reject`, body),
  /** Mock only: makes the shown prices stale so Approve returns 409. */
  simulatePriceChange: (id: string): Promise<void> => (USE_MOCK ? mock.simulatePriceChange(id) : Promise.resolve()),

  audit: (id: string): Promise<Audit> => (USE_MOCK ? mock.audit(id) : call(`/audit/${id}`)),
};
