import Link from "next/link";
import { day, dayTime, eur } from "@/lib/format";
import type { LimosRequest } from "@/lib/types";

export default function ApprovalBar({ req, busy, first, company, onApprove }: {
  req: LimosRequest;
  busy: boolean;
  first: string;
  company: string;
  onApprove: () => void;
}) {
  const q = req.quote!;
  const ordered = req.status === "ORDERED";
  const blocked = req.status === "BLOCKED" || q.policy.outcome === "BLOCK";
  const deadline = q.order_deadline ? `Order by ${dayTime(q.order_deadline)}` : "No order deadline from Valio";

  return (
    <section aria-label="Order approval" className="bg-petrol text-white rounded-xl px-4 py-3">
      {ordered ? (
        <div className="flex flex-wrap items-end justify-between gap-x-4 gap-y-1">
          <div className="min-w-0">
            <p role="status" className="m-0 text-[18px] font-semibold">Order placed</p>
            <p className="m-0 text-[17px]">{req.order?.approved_by ? `Thanks, ${first}.` : `Ordered automatically, within ${company}'s ordering limits.`}</p>
          </div>
          <Link href={`/audit/${req.request_id}`} className="min-h-11 flex items-center text-[16px] text-white underline">See order details</Link>
        </div>
      ) : blocked ? (
        <p className="m-0 text-[18px] font-semibold">Can&apos;t be ordered</p>
      ) : (
        <div className="grid grid-cols-[minmax(0,1fr)_auto] sm:grid-cols-[auto_minmax(0,1fr)_auto] items-end gap-x-3 sm:gap-x-8 gap-y-2">
          <div className="min-w-0">
            <p className="m-0 text-[13px] text-petrol-tint">Total incl. VAT</p>
            <p className="m-0 text-[26px] leading-tight font-semibold tabular-nums">{eur(q.total_with_tax)}</p>
          </div>
          <div className="hidden sm:block min-w-0">
            <p className="m-0 text-[13px] text-petrol-tint">Delivery</p>
            <p className="m-0 text-[26px] leading-tight font-semibold">{day(q.delivery_date)} <span className="text-[15px] text-petrol-tint font-normal">| {deadline}</span></p>
          </div>
          {req.status === "WAITING_APPROVAL" && req.approval_id && (
            <button type="button" onClick={onApprove} disabled={busy} className="row-start-1 col-start-2 sm:col-start-3 min-h-12 px-5 rounded-md bg-lime text-petrol-dark text-[17px] font-semibold disabled:opacity-60">Approve order</button>
          )}
          <p className="sm:hidden col-span-2 m-0 text-[14px] text-petrol-tint">Delivery {day(q.delivery_date)} | {deadline}</p>
        </div>
      )}
    </section>
  );
}
