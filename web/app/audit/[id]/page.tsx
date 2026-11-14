"use client";
import Link from "next/link";
import { use, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { dayTime } from "@/lib/format";
import { reasonLabel } from "@/components/OrderLine";
import type { Audit, Step } from "@/lib/types";

const TOOLS: Record<string, string> = {
  request_quote: "Asked Valio Aimo for today's prices and stock",
  check_offers: "Checked prices and stock with Valio Aimo",
  search_products: "Searched the Valio Aimo catalogue",
  place_order: "Sent the order to Valio Aimo",
};

/** Plain words for the gateway's log steps. Falls back to the step's own title. */
function plain(s: Step): string {
  const d = (s.detail ?? {}) as Record<string, unknown>;
  switch (s.type) {
    case "REQUEST_RECEIVED":
      return s.title === "Request received" ? "Asked for a new list" : s.title;
    case "TOOL_CALL":
      return TOOLS[String(d.tool)] ?? s.title;
    case "QUOTE_CREATED":
      return typeof d.total === "number" ? `Locked the prices: ${d.total.toFixed(2)} EUR excl. VAT` : s.title;
    case "POLICY_DECISION": {
      if (!d.outcome) return s.title;
      const rules = Array.isArray(d.rules) ? (d.rules as unknown[]) : [];
      const why = rules
        .map((r) => reasonLabel(typeof r === "string" ? r : String((r as { rule_id?: string }).rule_id)).toLowerCase())
        .join(", ");
      if (d.outcome === "AUTO") return "Can be ordered without a check: no rule needed a person";
      if (d.outcome === "BLOCK") return `Can't be ordered: ${why || "a rule stopped it"}`;
      return `Needs a check before ordering${why ? `: ${why}` : ""}`;
    }
    case "ORDER_PLACED":
      return d.simulated ? "Order placed with Valio Aimo (simulated for the demo)" : s.title;
    default:
      return s.title;
  }
}

const WHO: Record<string, string> = {
  user: "You",
  customer: "You",
  agent: "Limos assistant",
  gateway: "Limos",
  policy: "Ordering rules",
  human: "Approved by",
};

export default function AuditPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [audit, setAudit] = useState<Audit | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.audit(id).then(setAudit);
  }, [id]);

  if (!audit) return <main className="max-w-[860px] mx-auto px-4 sm:px-6 py-10 text-muted">Loading…</main>;

  return (
    <main className="max-w-[860px] mx-auto px-4 sm:px-6 pt-8 pb-12 flex flex-col gap-6">
      <Link href="/" className="min-h-11 inline-flex items-center text-petrol underline self-start">
        Back to my order
      </Link>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-[30px] font-bold tracking-tight m-0">Order details</h1>
        <button
          onClick={() => {
            navigator.clipboard.writeText(JSON.stringify(audit, null, 2)).then(() => setCopied(true));
          }}
          className="min-h-12 px-5 rounded-full border-2 border-petrol text-petrol font-bold bg-white"
        >
          {copied ? "Copied" : "Copy as JSON"}
        </button>
      </div>

      <div className={`rounded-2xl px-5 py-4 ${audit.chain_valid ? "bg-good-bg" : "bg-bad-bg text-bad"}`}>
        <strong>{audit.chain_valid ? "Checked: nothing in this record has been changed." : "Warning: this record has been changed after it was written."}</strong>
        <span className="block text-[16px] text-soft">
          Every step is linked to the one before it, so an edit afterwards shows up here.
        </span>
      </div>

      <ol className="bg-white rounded-2xl shadow-card px-5 py-2 m-0 list-none">
        {audit.steps.map((s) => (
          <li key={s.seq} className="py-3.5 border-t border-hair first:border-t-0 flex gap-4">
            <span className="hidden sm:block flex-none w-16 text-muted text-[15px] tabular-nums">{dayTime(s.at).split(", ")[1]}</span>
            <span className="flex-1 min-w-0">
              <span className="block text-[14px] font-semibold text-soft">
                {WHO[s.actor] ?? s.actor}
                {/* On a phone the time moves up here to leave the full width for the step. */}
                <span className="sm:hidden font-normal text-muted tabular-nums"> · {dayTime(s.at).split(", ")[1]}</span>
              </span>
              {plain(s)}
            </span>
          </li>
        ))}
      </ol>
    </main>
  );
}
