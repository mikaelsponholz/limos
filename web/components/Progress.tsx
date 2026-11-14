"use client";
// What Limos is doing while a request runs. A real reorder takes about 7 to 20 s (Valio
// calls), so the stages move on with the gateway's steps, and with time when no new step arrives.
import { useEffect, useState } from "react";
import type { Step } from "@/lib/types";

const STAGES: { label: string; types: string[]; after: number }[] = [
  { label: "Reading your order", types: ["REQUEST_RECEIVED", "CUSTOMER_LOADED", "INTENT_PARSED", "CLARIFICATION_ANSWERED", "SUGGESTION_ACCEPTED"], after: 0 },
  { label: "Finding the products at Valio Aimo", types: ["EVENT_EXPANDED", "TOOL_CALL", "CANDIDATES", "CHOICE", "EXCEPTION", "SUBSTITUTE_PROPOSED"], after: 1.5 },
  { label: "Checking today's prices and delivery", types: ["QUOTE_CREATED", "REQUOTED", "AVAILABILITY_RECHECKED"], after: 6 },
  // Only a real step reaches the last stage, so time alone never says the rules are being checked.
  { label: "Checking your rules", types: ["POLICY_DECISION", "APPROVAL_REQUESTED"], after: Infinity },
];

export default function Progress({ steps, startedAt }: { steps: Step[]; startedAt: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(t);
  }, []);

  const seconds = (now - startedAt) / 1000;
  const byTime = STAGES.reduce((at, s, i) => (seconds >= s.after ? i : at), 0);
  const bySteps = STAGES.reduce((at, s, i) => (steps.some((st) => s.types.includes(st.type)) ? i : at), 0);
  const current = Math.max(byTime, bySteps);

  return (
    <section className="bg-surface rounded-xl border border-hair px-5 py-4 flex flex-col gap-3">
      <ol className="m-0 p-0 list-none flex flex-col gap-2.5 text-[17px]">
        {STAGES.map((s, i) => (
          <li key={s.label} className={`flex items-center gap-3 ${i > current ? "text-muted" : ""}`}>
            {i < current ? (
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden className="flex-none text-good">
                <path d="M5 12.5l4.5 4.5L19 7.5" />
              </svg>
            ) : i === current ? (
              <span aria-hidden className="flex-none w-[22px] h-[22px] rounded-full border-[3px] border-petrol-ring border-t-petrol motion-safe:animate-spin" />
            ) : (
              <span aria-hidden className="flex-none w-[22px] h-[22px] rounded-full border-2 border-line" />
            )}
            <span className={i === current ? "font-semibold" : ""} aria-current={i === current ? "step" : undefined}>
              {s.label}
              {i < current && <span className="sr-only"> (done)</span>}
            </span>
          </li>
        ))}
      </ol>
      <p role="status" aria-live="polite" className="m-0 text-[15px] text-muted">
        <span className="sr-only">{STAGES[current].label}. </span>
        {seconds < 4 ? "" : seconds < 25 ? "This usually takes 10 to 20 seconds." : "Valio Aimo is a bit slow right now. Still working on it."}
      </p>
    </section>
  );
}
