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
      <span className="flex-none w-10 h-10 sm:w-12 sm:h-12 rounded-xl flex items-center justify-center" style={{ background: c.tile }}>
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke={c.ink} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d={c.icon} />
        </svg>
      </span>
      <span className="flex-1 min-w-0 flex flex-col">
        <span className="font-semibold leading-snug">{l.name}</span>
        <span className="text-[15px] text-soft">
          {l.quantity_text} · {eur(l.unit_price)} each
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

const REASONS: Record<string, string> = {
  SPLIT_DELIVERY: "Split into two sizes",
  SUBSTITUTION: "Replacement product",
  BUDGET_EXCEEDED: "Over budget",
  OVER_AUTO_LIMIT: "Over your auto limit",
  DIETARY_UNKNOWN: "Diet not confirmed",
  ALLERGEN_RISK: "Allergen risk",
};

/** Short plain label for a rule id (the full sentence is in the quote's policy). */
export const reasonLabel = (id: string) =>
  REASONS[id] ?? id.charAt(0) + id.slice(1).toLowerCase().replace(/_/g, " ");
