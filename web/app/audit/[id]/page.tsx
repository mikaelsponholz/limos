"use client";
import Link from "next/link";
import { use, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { dayTime } from "@/lib/format";
import type { Audit } from "@/lib/types";

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

  if (!audit) return <main className="max-w-[860px] mx-auto px-6 py-10 text-muted">Loading…</main>;

  return (
    <main className="max-w-[860px] mx-auto px-6 pt-8 pb-12 flex flex-col gap-6">
      <Link href="/" className="text-petrol underline self-start">
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
            <span className="flex-none w-16 text-muted text-[15px] tabular-nums">{dayTime(s.at).split(", ")[1]}</span>
            <span className="flex-1">
              <span className="block text-[14px] font-semibold text-soft">{WHO[s.actor] ?? s.actor}</span>
              {s.title}
            </span>
          </li>
        ))}
      </ol>
    </main>
  );
}
