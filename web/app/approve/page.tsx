"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { reasonLabel } from "@/components/OrderLine";
import { api } from "@/lib/api";
import { dayTime } from "@/lib/format";
import type { ApprovalSummary } from "@/lib/types";

function minutesLeft(iso: string) {
  return Math.round((new Date(iso).getTime() - Date.now()) / 60_000);
}

export default function ApprovalsInbox() {
  const [items, setItems] = useState<ApprovalSummary[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const load = () =>
      api
        .approvals()
        .then((a) => {
          setItems(a);
          setError(false);
        })
        .catch(() => setError(true));
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  return (
    <main className="max-w-[640px] mx-auto px-4 sm:px-6 pt-8 pb-12 flex flex-col gap-5">
      <h1 className="text-[30px] font-bold tracking-tight m-0">Waiting for your approval</h1>
      {error && <p className="bg-bad-bg text-bad rounded-xl px-4 py-3">We could not load the approvals. Trying again…</p>}
      {items === null && !error && <p className="text-muted">Loading…</p>}
      {items?.length === 0 && (
        <p className="bg-white rounded-2xl shadow-card px-5 py-6 text-soft">Nothing is waiting for you right now.</p>
      )}
      {items?.map((a) => {
        const left = minutesLeft(a.expires_at);
        return (
          <Link
            key={a.approval_id}
            href={`/approve/${a.approval_id}`}
            className="bg-white rounded-2xl shadow-card px-5 py-4 flex flex-col gap-2 no-underline text-ink hover:ring-2 hover:ring-petrol-ring"
          >
            <span className="text-lg font-semibold leading-snug">{a.summary}</span>
            <span className="flex flex-wrap gap-2">
              {a.reasons.map((r) => (
                <span key={r} className="bg-warn-bg text-warn rounded-full px-3 py-0.5 text-[15px] font-semibold">
                  {reasonLabel(r)}
                </span>
              ))}
            </span>
            <span className="text-[15px] text-soft">
              {a.order_deadline ? `Order by ${dayTime(a.order_deadline)}` : "No order deadline from Valio"}
              {" · "}
              {left > 0 ? `prices held for ${left} min` : "prices expired, will be checked again"}
            </span>
          </Link>
        );
      })}
    </main>
  );
}
