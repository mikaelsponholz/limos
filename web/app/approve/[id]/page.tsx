"use client";
import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import OrderLine from "@/components/OrderLine";
import { USE_MOCK, api } from "@/lib/api";
import { useCustomer } from "@/lib/customer";
import { day, dayTime, eur } from "@/lib/format";
import { ApiError, type ApprovalDetail, type Quote } from "@/lib/types";

const NAME_KEY = "limos.approver";

function storedName(): string {
  try {
    return localStorage.getItem(NAME_KEY) ?? "";
  } catch {
    return "";
  }
}

export default function ApprovalCard({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { customer } = useCustomer();
  const [a, setA] = useState<ApprovalDetail | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ text: string; reload?: boolean } | null>(null);
  const [orderRequest, setOrderRequest] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .approval(id)
      .then((d) => {
        setA(d);
        setPicked((p) => p ?? d.quote.quote_id);
      })
      .catch(() => setError({ text: "We could not find this approval." }));
  }, [id]);
  useEffect(load, [load]);
  useEffect(() => setName(storedName() || customer?.name || ""), [customer?.name]);

  if (!a) {
    return (
      <main className="max-w-[480px] mx-auto px-4 py-10 text-muted">
        {error ? <p className="bg-bad-bg text-bad rounded-xl px-4 py-3">{error.text}</p> : "Loading…"}
      </main>
    );
  }

  const options: Quote[] = a.options.length ? a.options : [a.quote];
  const q = options.find((o) => o.quote_id === picked) ?? a.quote;
  const rules = a.quote.policy.rules_fired;

  const remember = () => {
    try {
      localStorage.setItem(NAME_KEY, name.trim());
    } catch {
      /* private mode */
    }
  };

  const approve = async () => {
    if (!name.trim()) return setError({ text: "Please write your name first." });
    remember();
    setBusy(true);
    setError(null);
    try {
      const res = await api.approve(a.approval_id, { quote_id: q.quote_id, content_hash: q.content_hash, approver: name.trim() });
      setOrderRequest(res.order.request_id);
      load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.code === "QUOTE_CHANGED")
        setError({ text: "This list changed after you opened it. Please look at it again.", reload: true });
      else if (e instanceof ApiError && e.status === 410) setError({ text: "The prices expired. A new list will be made." });
      else if (e instanceof ApiError && e.status === 409) setError({ text: "Someone already decided this order.", reload: true });
      else setError({ text: "That did not go through. Please try again." });
    } finally {
      setBusy(false);
    }
  };

  const reject = async () => {
    if (!name.trim()) return setError({ text: "Please write your name first." });
    remember();
    setBusy(true);
    try {
      await api.reject(a.approval_id, { approver: name.trim() });
      load();
    } catch {
      setError({ text: "That did not go through. Please try again." });
    } finally {
      setBusy(false);
    }
  };

  if (a.status !== "PENDING") {
    const ok = a.status === "APPROVED";
    return (
      <main className="max-w-[480px] mx-auto px-4 pt-8 pb-12 flex flex-col gap-4">
        <div className={`rounded-2xl px-5 py-5 flex flex-col gap-1 ${ok ? "bg-good-bg" : "bg-white shadow-card"}`}>
          <strong className="text-2xl">{ok ? "Order placed" : a.status === "REJECTED" ? "Rejected" : "Expired"}</strong>
          <span>
            {ok
              ? `${a.request.title}. ${eur(q.total_with_tax)}, arrives ${day(q.delivery_date)}.`
              : "Nothing was ordered. Your decision is saved."}
          </span>
          {a.decided_by && <span className="text-[15px] text-soft">Decided by {a.decided_by}.</span>}
        </div>
        <Link href={`/audit/${orderRequest ?? a.request.request_id}`} className="text-petrol underline">
          See order details
        </Link>
        <Link href="/approve" className="text-petrol underline">
          Back to approvals
        </Link>
      </main>
    );
  }

  return (
    <main className="max-w-[480px] mx-auto px-4 pt-6 pb-12 flex flex-col gap-5">
      <div className="flex flex-col gap-1">
        <span className="text-[15px] font-bold text-warn">Needs your approval</span>
        <h1 className="text-[28px] leading-tight font-bold tracking-tight m-0">{a.request.title}</h1>
        <span className="text-soft">
          Arrives {day(q.delivery_date)}
          {q.order_deadline ? ` · order by ${dayTime(q.order_deadline)}` : ""}
        </span>
      </div>

      {rules.length > 0 && (
        <section className="bg-warn-bg rounded-2xl px-5 py-4 flex flex-col gap-2">
          <h2 className="m-0 text-lg font-bold text-warn">Why you are asked</h2>
          <ul className="m-0 pl-5 flex flex-col gap-1.5">
            {rules.map((r) => (
              <li key={r.rule_id + (r.product_id ?? "")}>{r.description}</li>
            ))}
          </ul>
        </section>
      )}

      {options.length > 1 && (
        <fieldset className="border-0 m-0 p-0 flex flex-col gap-2">
          <legend className="text-lg font-bold mb-2">Choose one</legend>
          {options.map((o, i) => {
            const on = o.quote_id === q.quote_id;
            return (
              <button
                key={o.quote_id}
                type="button"
                aria-pressed={on}
                onClick={() => setPicked(o.quote_id)}
                className={`text-left rounded-2xl bg-white px-4 py-3.5 flex gap-3 items-start ${on ? "ring-2 ring-petrol" : "border border-line"}`}
              >
                <span className={`mt-1 w-5 h-5 rounded-full border-2 flex items-center justify-center ${on ? "border-petrol" : "border-muted"}`}>
                  {on && <span className="w-2.5 h-2.5 rounded-full bg-petrol" />}
                </span>
                <span className="flex-1">
                  <span className="font-semibold leading-snug block">{o.option_label ?? `Option ${i + 1}`}</span>
                  {i === 0 && <span className="text-[14px] text-good font-semibold">Recommended</span>}
                </span>
                <span className="font-bold tabular-nums whitespace-nowrap">{eur(o.total_with_tax)}</span>
              </button>
            );
          })}
        </fieldset>
      )}

      <section className="bg-white rounded-2xl shadow-card px-4 py-2">
        <h2 className="mt-3 mb-1 text-lg font-bold">In this order</h2>
        {q.lines.map((l) => (
          <OrderLine key={l.product_id} l={l} />
        ))}
        <div className="flex justify-between items-baseline border-t-2 border-ink py-3">
          <span className="font-bold">Total incl. VAT</span>
          <span className="text-[28px] font-extrabold tabular-nums">{eur(q.total_with_tax)}</span>
        </div>
      </section>

      {q.agent_explanation && (
        <section className="flex flex-col gap-1">
          <h2 className="m-0 text-lg font-bold">Why this list</h2>
          <p className="m-0 text-soft leading-relaxed">{q.agent_explanation}</p>
        </section>
      )}

      <label className="flex flex-col gap-1.5 font-semibold">
        Your name
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="font-normal text-lg px-3.5 py-3 rounded-xl border-2 border-line bg-white"
        />
      </label>

      {error && (
        <div className="bg-bad-bg text-bad rounded-xl px-4 py-3 flex flex-col gap-2">
          {error.text}
          {error.reload && (
            <button
              onClick={() => {
                setError(null);
                setPicked(null);
                load();
              }}
              className="self-start underline font-semibold"
            >
              Show the current list
            </button>
          )}
        </div>
      )}

      <div className="flex flex-col gap-2.5">
        <button
          onClick={approve}
          disabled={busy}
          className="min-h-[60px] rounded-full bg-lime text-petrol-dark text-lg font-extrabold disabled:opacity-60"
        >
          Approve order · {eur(q.total_with_tax)}
        </button>
        <button
          onClick={reject}
          disabled={busy}
          className="min-h-[52px] rounded-full border-2 border-petrol text-petrol font-bold bg-white disabled:opacity-60"
        >
          Reject
        </button>
      </div>
      <p className="m-0 text-sm text-muted">You approve exactly this list and these prices. If anything changes, you are asked again.</p>

      {USE_MOCK && (
        <button
          onClick={() => api.simulatePriceChange(a.approval_id)}
          className="self-start text-sm text-muted underline"
        >
          Demo: prices change in the background
        </button>
      )}
    </main>
  );
}
