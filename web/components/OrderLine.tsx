import { categoryOf, day, dayTime, eur, isPast } from "@/lib/format";
import type { Offer } from "@/lib/types";

/** One shopping-list row: product, quantity in plain words, delivery date and order deadline. */
export default function OrderLine({ l }: { l: Offer }) {
  const c = categoryOf(l.name);
  const note = l.is_substitute
    ? l.substitute_source === "valio"
      ? "Valio's suggested replacement"
      : "Suggested replacement"
    : l.is_split
      ? "Second size, to cover the full amount"
      : l.flags.includes("PARTIAL")
        ? "Not enough in stock for all of it"
        : null;
  const deadline = !l.order_deadline
    ? "no order deadline from Valio"
    : isPast(l.order_deadline)
      ? "order deadline has passed"
      : `order by ${dayTime(l.order_deadline)}`;
  return (
    <div className="flex items-center gap-3 sm:gap-3.5 py-3.5 border-t border-hair">
      {/* The tile is decoration; on a phone the product name needs the room more. */}
      <span className="hidden sm:flex flex-none w-12 h-12 rounded-xl items-center justify-center" style={{ background: c.tile }}>
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke={c.ink} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d={c.icon} />
        </svg>
      </span>
      <span className="flex-1 min-w-0 flex flex-col">
        <span className="font-semibold leading-snug">{l.name}</span>
        <span className="text-[15px] text-soft">
          {/* The gateway writes whole numbers as "2.0 x 1.45 kg"; show "2 x 1.45 kg". */}
          {l.quantity_text.replace(/\b(\d+)\.0\b/g, "$1")} · {eur(l.unit_price)} each
        </span>
        {note && <span className="text-[15px] text-warn">{note}</span>}
        <span className="text-[14px] text-muted">
          Arrives {day(l.delivery_date)}, {deadline}
        </span>
      </span>
      <span className="flex-none text-right font-bold tabular-nums whitespace-nowrap">{eur(l.line_total)}</span>
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
