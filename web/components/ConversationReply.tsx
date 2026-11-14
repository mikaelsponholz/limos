"use client";
import { useState } from "react";
import OrderLine from "@/components/OrderLine";
import { assumedLines } from "@/lib/assumed";
import { day, eur } from "@/lib/format";
import type { LimosRequest, Offer, Suggestion } from "@/lib/types";

const products = (n: number) => `${n} ${n === 1 ? "product" : "products"}`;

function words(parts: string[]) {
  return parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}` : parts[0] ?? "";
}

function changes(previous: Offer[], lines: Offer[]) {
  const before = new Map(previous.map((line) => [line.product_id, line]));
  const after = new Map(lines.map((line) => [line.product_id, line]));
  const added = lines.filter((line) => !before.has(line.product_id)).map((line) => line.name);
  const removed = previous.filter((line) => !after.has(line.product_id)).map((line) => line.name);
  const changed = lines.filter((line) => {
    const old = before.get(line.product_id);
    return old && (old.quantity_sales_units !== line.quantity_sales_units || old.quantity_text !== line.quantity_text);
  }).map((line) => `${line.name} to ${line.quantity_text}`);
  const parts = [added.length ? `Added ${words(added)}` : "", removed.length ? `Removed ${words(removed)}` : "", changed.length ? `Changed ${words(changed)}` : ""].filter(Boolean);
  return parts.length ? `${parts.join("; ")}.` : "The products are the same as before.";
}

export default function ConversationReply({ req, previousLines, latest, busy, onAccept, onAnswer }: {
  req: LimosRequest;
  previousLines?: Offer[];
  latest: boolean;
  busy: boolean;
  onAccept: (s: Suggestion) => void;
  onAnswer: (value: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const q = req.quote;
  if (req.status === "FAILED") return <p role="alert" className="m-0 text-[17px] text-bad">{req.error?.message ?? "Something went wrong."}</p>;
  if (req.status === "NEEDS_INPUT" && req.question) {
    const question = req.question;
    return (
      <div className="flex flex-col gap-4">
        <p className="m-0 text-[17px] text-ink">{question.text}</p>
        {latest && <>
          <div className="flex flex-wrap gap-3">
            {question.choices.map((choice) => (
              <button key={choice} type="button" disabled={busy} onClick={() => onAnswer(choice)} className="min-h-14 min-w-24 max-w-full break-words px-6 rounded-md border border-petrol text-petrol text-[22px] font-semibold hover:bg-petrol-tint">{choice}</button>
            ))}
          </div>
          {question.default && <button type="button" disabled={busy} onClick={() => onAnswer(null)} className="self-start text-petrol underline min-h-11">Use default ({question.default})</button>}
        </>}
      </div>
    );
  }
  if (!q) return null;
  const ordered = req.status === "ORDERED";
  const blocked = req.status === "BLOCKED" || q.policy.outcome === "BLOCK";
  const rules = q.policy.rules_fired;
  const generalRules = rules.filter((rule) => !q.lines.some((line) => line.product_id === rule.product_id));
  const linesToCheck = q.lines.filter((line) => rules.some((rule) => rule.product_id === line.product_id)).length;
  const offered = req.suggestions.filter((suggestion) => suggestion.status === "OFFERED");
  const assumptions = assumedLines(req.intent?.filled_gaps ?? []).map(({ text }) => {
    const clean = text.replace(/^We assumed:\s*/i, "").replace(/\.+$/, "").trim();
    const lower = clean.charAt(0).toLowerCase() + clean.slice(1);
    // Read as one sentence: "the same as ...", and keep "from your settings" from looking like a list item.
    return lower.replace(/^same as /, "the same as ").replace(/, from your settings$/, " (from your settings)");
  }).filter(Boolean);
  const expanded = latest || open;
  // The gateway ranks insights; the chat shows the top three as tips.
  const tips = (req.insights ?? []).slice(0, 3);
  const listId = `list-${req.request_id}`;

  return (
    <div className="flex flex-col gap-4 min-w-0" aria-busy={latest && busy}>
      <p className="m-0 text-[17px] text-ink">
        {previousLines ? `Updated your list: ${products(q.lines.length)}, total ${eur(q.total_with_tax)}. ${changes(previousLines, q.lines)}` : `Here is your list: ${products(q.lines.length)}, delivered ${day(q.delivery_date)}.${assumptions.length ? ` I assumed ${words(assumptions)}.` : ""}`}
      </p>
      {!latest && (
        <div className="flex items-center justify-between gap-3 border-t border-hair pt-2 -mb-2 text-[16px] text-soft">
          <span>List | {products(q.lines.length)} | {eur(q.total_with_tax)}</span>
          <button type="button" aria-expanded={open} aria-controls={listId} onClick={() => setOpen((value) => !value)} className="min-h-11 text-petrol font-medium hover:underline">{open ? "Hide" : "Show"}</button>
        </div>
      )}
      <div id={listId} hidden={!expanded} className={`${expanded ? "flex" : "hidden"} flex-col gap-4 min-w-0`}>
        <section aria-label="Product list" className="border-t border-hair min-w-0">
          <div className="divide-y divide-hair">{q.lines.map((line) => <OrderLine key={line.product_id} l={line} rules={rules} />)}</div>
        </section>
        {!ordered && linesToCheck > 0 && <p className="m-0 text-[17px] text-ink">{linesToCheck} {linesToCheck === 1 ? "line needs" : "lines need"} a look before you approve.</p>}
        {!ordered && generalRules.length > 0 && (
          <section className="border-t border-hair">
            <h2 className="mt-3 mb-1 text-[18px] font-semibold">{blocked ? "Why it can't be ordered" : "Check before you approve"}</h2>
            <ul className="m-0 p-0 list-none divide-y divide-hair">
              {generalRules.map((rule, i) => <li key={`${rule.rule_id}-${i}`} className={`py-3 text-[17px] ${blocked ? "text-bad" : "text-ink"}`}>{rule.description}</li>)}
            </ul>
          </section>
        )}
      </div>
      {latest && tips.length > 0 && (
        <section aria-label="Price tips" className="border-t border-hair">
          <h2 className="mt-3 mb-1 text-[18px] font-semibold">Price tips</h2>
          <ul className="m-0 p-0 list-none divide-y divide-hair">
            {tips.map((tip, i) => (
              <li key={`${tip.kind}-${i}`} className="py-3 flex flex-wrap items-baseline justify-between gap-3">
                <p className="m-0 flex-[1_1_280px] min-w-0 text-[17px] text-ink">{tip.text}</p>
                {tip.saving !== null && tip.saving > 0 && <span className="font-semibold tabular-nums text-petrol">{tip.kind === "waste" ? `${eur(tip.saving)} unused` : `save ${eur(tip.saving)}`}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}
      {latest && offered.length > 0 && !ordered && (
        <section className="border-t border-hair">
          <h2 className="mt-3 mb-1 text-[18px] font-semibold">If you want</h2>
          <ul className="m-0 p-0 list-none divide-y divide-hair">
            {offered.map((s) => {
              const replaced = s.replaces_item_id ? q.lines.find((line) => line.item_id === s.replaces_item_id) : null;
              const delta = s.offer ? s.offer.line_total_with_tax - (replaced?.line_total_with_tax ?? 0) : null;
              return (
                <li key={s.suggestion_id} className="py-4 flex flex-wrap items-center justify-between gap-3">
                  <div className="flex-[1_1_280px] min-w-0"><p className="m-0 font-medium">{s.title}</p><p className="m-0 text-[16px] text-soft">{s.reason}</p></div>
                  {delta !== null && <span className="font-semibold tabular-nums">{delta < 0 ? "−" : "+"}{eur(Math.abs(delta))}</span>}
                  <button type="button" disabled={busy} onClick={() => onAccept(s)} className="min-h-11 px-5 rounded-md border border-petrol text-petrol font-semibold hover:bg-petrol-tint">{s.replaces_item_id ? "Swap" : "Add"}</button>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
