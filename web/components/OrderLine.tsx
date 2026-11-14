import { day, dayTime, eur, isPast } from "@/lib/format";
import type { Offer, RuleFired } from "@/lib/types";

/** One shopping-list row: product, quantity in plain words, delivery date and order deadline. */
export default function OrderLine({ l, rules = [] }: { l: Offer; rules?: RuleFired[] }) {
  const lineRules = rules.filter((r) => r.product_id === l.product_id);
  const valioSubstitute = l.is_substitute && l.substitute_source === "valio";
  const substitution = lineRules.find((r) => r.rule_id === "SUBSTITUTION");
  const otherRules = lineRules.filter((r) => !valioSubstitute || r.rule_id !== "SUBSTITUTION");
  const notes = [
    l.is_split && !lineRules.some((r) => r.rule_id === "SPLIT_DELIVERY") ? "Second size, to cover the full amount" : null,
    l.flags.includes("PARTIAL") && !lineRules.some((r) => r.rule_id === "PARTIAL_QUANTITY") ? "Not enough in stock for all of it" : null,
    l.is_substitute && !valioSubstitute && !substitution ? "Suggested replacement" : null,
  ].filter(Boolean);
  const deadline = !l.order_deadline
    ? "No order deadline from Valio"
    : isPast(l.order_deadline)
      ? "order deadline has passed"
      : `Order by ${dayTime(l.order_deadline)}`;
  return (
    <div className="flex flex-col gap-1 py-3.5 min-w-0">
      <div className="flex justify-between items-baseline gap-3">
        <span className="text-[18px] font-medium leading-snug min-w-0 break-words">{l.name}</span>
        <span className="flex-none text-[18px] font-semibold tabular-nums whitespace-nowrap">{eur(l.line_total)}</span>
      </div>
      <span className="text-[16px] text-soft">
        {/* The gateway writes whole numbers as "2.0 x 1.45 kg"; show "2 x 1.45 kg". */}
        {l.quantity_text.replace(/\b(\d+)\.0\b/g, "$1")} | {eur(l.unit_price)} each
      </span>
      <span className="text-[16px] text-soft">
        Arrives {day(l.delivery_date)} | <span className={isPast(l.order_deadline) ? "text-bad" : undefined}>{deadline}</span>
      </span>
      {otherRules.map((r, i) => <span key={`${r.rule_id}-${i}`} className="text-[16px] text-warn">{r.description}</span>)}
      {notes.map((note) => <span key={note} className="text-[16px] text-warn">{note}</span>)}
      {valioSubstitute && (
        <p className="m-0 mt-2 bg-petrol text-white rounded-md px-3.5 py-2.5 text-[16px]">
          <strong className="font-semibold">Valio substitute.</strong> {substitution?.description ?? "Valio's suggested replacement for a product that can't be delivered."}
        </p>
      )}
    </div>
  );
}

// Every RuleId in gateway/schemas.py, in words Pena understands.
const REASONS: Record<string, string> = {
  AGENT_NOT_PERMITTED: "Not allowed to order for you",
  SALES_BLOCK: "Not on sale right now",
  NOT_DELIVERABLE: "Can't be delivered",
  PRICE_MISSING: "No price from Valio",
  ALLERGEN_CONTAINS: "Contains an allergen",
  DIETARY_VIOLATED: "Doesn't fit your diet",
  SPEND_HARD_CAP: "Over your spending cap",
  BUDGET_HARD_STOP: "Far over budget",
  ALLERGEN_MAY_CONTAIN: "May contain an allergen",
  ALLERGEN_DATA_MISSING: "No allergen information",
  DIETARY_UNKNOWN: "Diet not confirmed",
  PREFERENCE_REQUIRED_UNMET: "Your wish not confirmed",
  SPEND_SOFT_LIMIT: "Bigger order than usual",
  LINE_SOFT_LIMIT: "One product costs a lot",
  BUDGET_EXCEEDED: "Over budget",
  LATE_DELIVERY: "Arrives later than asked",
  PARTIAL_QUANTITY: "Not enough in stock",
  SPLIT_DELIVERY: "Split into two sizes",
  SUBSTITUTION: "Replacement product",
  OVERBUY: "More than you asked for",
  PACK_SIZE_UNKNOWN: "Pack size not known",
  TERMINAL_LOW_RELIABILITY: "Delivery may be late",
  PRICE_CHANGED: "Price changed",
  CUTOFF_PASSED: "Order deadline passed",
  APPROVAL_ALWAYS_REQUIRED: "Always needs your OK",
};

/** Short plain label for a rule id (the full sentence is in the quote's policy). */
export const reasonLabel = (id: string) =>
  REASONS[id] ?? id.charAt(0) + id.slice(1).toLowerCase().replace(/_/g, " ");
