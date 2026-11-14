"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useCustomer } from "@/lib/customer";
import { readSettings, requestOverrides } from "@/lib/settings";
import { assumedLines } from "@/lib/assumed";
import OrderLine from "@/components/OrderLine";
import Progress from "@/components/Progress";
import { ICONS, day, dayTime, eur } from "@/lib/format";
import { ApiError, type LimosRequest, type Step, type Suggestion } from "@/lib/types";

const POLL_MS = 1500;

export default function OrderPage() {
  const { customer, reload } = useCustomer();
  const [text, setText] = useState("");
  const [req, setReq] = useState<LimosRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set while a list is being made: the gateway's steps so far, and the past order tapped (if any).
  const [running, setRunning] = useState<{ steps: Step[]; startedAt: number; reorderOf?: string } | null>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // A new customer starts a fresh page.
  useEffect(() => {
    setReq(null);
    setText("");
    setError(null);
    setRunning(null);
  }, [customer?.customer_id]);

  const poll = useCallback((id: string) => {
    if (timer.current) clearTimeout(timer.current);
    const tick = async () => {
      try {
        const r = await api.getRequest(id);
        if (r.status === "RUNNING") {
          setBusy(true);
          setRunning((p) => (p ? { ...p, steps: r.steps ?? [] } : p));
          timer.current = setTimeout(tick, POLL_MS);
          return;
        }
        setReq(r);
        setBusy(false);
        setRunning(null);
      } catch (e) {
        setBusy(false);
        setRunning(null);
        setError(e instanceof ApiError ? e.message : "Something went wrong. Please try again.");
      }
    };
    tick();
  }, []);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const start = async (body: { text?: string; reorder_of?: string }) => {
    if (!customer) return;
    setError(null);
    setBusy(true);
    setRunning({ steps: [], startedAt: Date.now(), reorderOf: body.reorder_of });
    try {
      const { request_id } = await api.createRequest({
        customer_id: customer.customer_id,
        ...requestOverrides(readSettings(customer.customer_id)),
        ...body,
      });
      poll(request_id);
    } catch {
      setBusy(false);
      setRunning(null);
      setError("We could not reach Limos. Please try again.");
    }
  };

  const answer = async (value: string | null) => {
    if (!req?.question) return;
    setBusy(true);
    setRunning({ steps: [], startedAt: Date.now() });
    await api.answer(req.request_id, req.question.question_id, value);
    poll(req.request_id);
  };

  const accept = async (s: Suggestion) => {
    if (!req) return;
    setBusy(true);
    setRunning({ steps: [], startedAt: Date.now() });
    await api.acceptSuggestion(req.request_id, s.suggestion_id);
    poll(req.request_id);
  };

  const approve = async () => {
    if (!req?.quote || !req.approval_id || !customer) return;
    setBusy(true);
    setError(null);
    try {
      await api.approve(req.approval_id, {
        quote_id: req.quote.quote_id,
        content_hash: req.quote.content_hash,
        approver: customer.name,
      });
      reload();
      poll(req.request_id);
    } catch (e) {
      setBusy(false);
      if (e instanceof ApiError && e.status === 409) {
        setError("Prices changed while you were looking. Here is the updated list.");
        poll(req.request_id);
      } else if (e instanceof ApiError && e.status === 410) {
        setError("This list expired. Checking prices again.");
        start({ text: req.input.text ?? undefined });
      } else setError("The order did not go through. Please try again.");
    }
  };

  if (!customer) return <main className="max-w-[1080px] mx-auto px-4 sm:px-6 py-10 text-muted">Loading…</main>;

  const first = customer.name.split(" ")[0];
  const history = customer.order_history.slice(0, 3);
  const last = history[0];

  return (
    <main className="max-w-[1080px] mx-auto px-4 sm:px-6 pt-8 pb-12 flex flex-col gap-6">
      <section className="flex flex-col gap-3">
        <h1 className="text-[30px] leading-tight font-bold tracking-tight">
          {last ? `Hi ${first}! Same as last time?` : `Hi ${first}, what would you like to order?`}
        </h1>
        <form
          className="bg-white rounded-2xl p-2 shadow-card flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) start({ text: text.trim() });
          }}
        >
          <textarea
            ref={field}
            rows={2}
            aria-label="Your order"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (text.trim()) start({ text: text.trim() });
              }
            }}
            placeholder={last ? `For example: same as last time, but for 40` : "For example: cakes for 30 on Friday"}
            className="flex-[1_1_420px] border-2 border-line rounded-xl px-3.5 py-3 text-[19px] leading-snug resize-none"
          />
          <button
            type="submit"
            disabled={busy || !text.trim()}
            className="min-h-14 px-6 rounded-xl bg-petrol text-white text-[17px] font-bold disabled:opacity-60"
          >
            {busy ? "Checking prices…" : "Make my list"}
          </button>
        </form>
        {/* Under the form for typed orders and new lists; a tapped past order shows it under that row. */}
        {running && !(running.reorderOf && !req) && <Progress steps={running.steps} startedAt={running.startedAt} />}
        {history.length > 0 && !req && (
          <section aria-labelledby="again-h" className="flex flex-col gap-2 mt-2">
            <h2 id="again-h" className="text-lg font-bold m-0">
              Order again
            </h2>
            <ol className="m-0 p-0 list-none flex flex-col gap-2">
              {history.map((o, i) => (
                <li key={o.order_ref}>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => start({ reorder_of: o.order_ref })}
                    className={`w-full text-left bg-white rounded-2xl shadow-card px-4 sm:px-5 py-3.5 min-h-16 flex items-center gap-3 sm:gap-4 hover:ring-2 hover:ring-petrol-ring ${
                      running?.reorderOf === o.order_ref ? "ring-2 ring-petrol" : "disabled:opacity-60"
                    }`}
                  >
                    <span className="flex-none w-9 h-9 rounded-full bg-petrol-tint text-petrol font-bold flex items-center justify-center">
                      {i + 1}
                    </span>
                    <span className="flex-1 min-w-0">
                      <span className="block font-semibold leading-snug">{o.title}</span>
                      <span className="block text-[15px] text-muted">
                        {running?.reorderOf === o.order_ref ? (
                          <span className="text-petrol font-semibold">Getting today&apos;s prices…</span>
                        ) : (
                          <>
                            Last ordered {day(o.date)}
                            {o.guests ? ` · ${o.guests} people` : ""}
                            {/* On a phone the price sits on this line, so the title gets the full width. */}
                            {o.total > 0 && <span className="sm:hidden font-bold text-ink tabular-nums"> · {eur(o.total)}</span>}
                          </>
                        )}
                      </span>
                    </span>
                    {o.total > 0 && <span className="hidden sm:block flex-none font-bold tabular-nums">{eur(o.total)}</span>}
                  </button>
                  {running?.reorderOf === o.order_ref && (
                    <div className="mt-2">
                      <Progress steps={running.steps} startedAt={running.startedAt} />
                    </div>
                  )}
                </li>
              ))}
            </ol>
            <p className="m-0 text-[15px] text-muted">Tap one to get it with today&apos;s prices, or write what you need above.</p>
          </section>
        )}
      </section>

      {error && <p className="bg-bad-bg text-bad rounded-xl px-4 py-3">{error}</p>}

      {req?.status === "NEEDS_INPUT" && req.question && (
        <section className="bg-white rounded-2xl shadow-card p-6 flex flex-col gap-4">
          <h2 className="text-2xl font-bold">{req.question.text}</h2>
          <div className="flex flex-wrap gap-3">
            {req.question.choices.map((c) => (
              <button
                key={c}
                disabled={busy}
                onClick={() => answer(c)}
                className="min-h-16 min-w-24 px-6 rounded-2xl border-2 border-petrol text-petrol text-2xl font-bold bg-white"
              >
                {c}
              </button>
            ))}
          </div>
          {req.question.default && (
            <button disabled={busy} onClick={() => answer(null)} className="self-start text-petrol underline font-semibold min-h-11">
              Use default ({req.question.default})
            </button>
          )}
        </section>
      )}

      {req?.quote && req.status !== "NEEDS_INPUT" && (
        <ListView req={req} busy={busy} onApprove={approve} onAccept={accept} onChange={() => field.current?.focus()} />
      )}

      {req?.status === "FAILED" && (
        <p className="bg-bad-bg text-bad rounded-xl px-4 py-3">{req.error?.message ?? "Something went wrong."}</p>
      )}
    </main>
  );
}

function ListView({
  req,
  busy,
  onApprove,
  onAccept,
  onChange,
}: {
  req: LimosRequest;
  busy: boolean;
  onApprove: () => void;
  onAccept: (s: Suggestion) => void;
  onChange: () => void;
}) {
  const q = req.quote!;
  const ordered = req.status === "ORDERED";
  const blocked = req.status === "BLOCKED" || q.policy.outcome === "BLOCK";
  const vat = q.total_with_tax - q.total;
  const gaps = assumedLines(req.intent?.filled_gaps ?? []);
  const offered = req.suggestions.filter((s) => s.status === "OFFERED");

  return (
    <div className={`flex flex-col gap-6 transition-opacity ${busy ? "opacity-60" : ""}`}>
      <div className="flex items-center gap-3 bg-good-bg rounded-xl px-4 py-3 text-[17px]">
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#3F6000" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d={ICONS.truck} />
        </svg>
        <span>
          Delivered <strong>{day(q.delivery_date)}</strong>
          {q.order_deadline ? (
            <>
              {" "}· Order by <strong>{dayTime(q.order_deadline)}</strong>
            </>
          ) : (
            " · No order deadline from Valio"
          )}
        </span>
      </div>

      {gaps.length > 0 && !ordered && (
        <ul className="flex flex-col gap-1 text-[17px] text-soft">
          {gaps.map((g, i) => (
            <li key={i}>
              {g.text}{" "}
              {g.toSettings ? (
                <Link href="/settings" className="inline-flex items-center min-h-11 -my-2.5 text-petrol underline font-semibold">
                  Change
                </Link>
              ) : (
                <button onClick={onChange} className="inline-flex items-center min-h-11 -my-2.5 text-petrol underline font-semibold">
                  Change
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-6 items-start">
        <section aria-labelledby="list-h" className="flex-[1_1_560px] min-w-0 flex flex-col gap-5">
          <div className="bg-white rounded-2xl shadow-card px-4 sm:px-5 py-2">
            <h2 id="list-h" className="mt-3 mb-1 text-xl font-bold">
              Your list <span className="font-medium text-muted text-base">· {q.lines.length} {q.lines.length === 1 ? "product" : "products"}</span>
            </h2>
            {q.lines.map((l) => (
              <OrderLine key={l.product_id} l={l} />
            ))}
          </div>

          {offered.length > 0 && !ordered && (
            <div className="flex flex-col gap-2.5">
              <h2 className="text-lg font-bold">If you want</h2>
              {offered.map((s) => {
                const replaced = s.replaces_item_id ? q.lines.find((l) => l.item_id === s.replaces_item_id) : null;
                const delta = s.offer ? s.offer.line_total_with_tax - (replaced?.line_total_with_tax ?? 0) : null;
                return (
                  <div key={s.suggestion_id} className="bg-white rounded-2xl shadow-card px-5 py-4 flex flex-wrap items-center justify-between gap-3">
                    <span className="flex-[1_1_320px]">
                      <span className="font-semibold">{s.title}</span>
                      <span className="block text-[15px] text-muted">{s.reason}</span>
                    </span>
                    {delta !== null && (
                      <span className="font-bold tabular-nums">
                        {delta <= 0 ? "−" : "+"}
                        {eur(Math.abs(delta))}
                      </span>
                    )}
                    <button
                      disabled={busy}
                      onClick={() => onAccept(s)}
                      className="min-h-12 px-5 rounded-full border-2 border-petrol text-petrol font-bold bg-white"
                    >
                      {s.replaces_item_id ? "Swap" : "Add"}
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <aside aria-label="Summary" className="w-full md:w-auto md:flex-[0_1_330px] md:min-w-[280px] md:sticky md:top-5 bg-white rounded-2xl shadow-card p-5 flex flex-col gap-3.5">
          <Row k="Delivery" v={day(q.delivery_date)} strong />
          {q.order_deadline && <Row k="Order by" v={dayTime(q.order_deadline)} strong />}
          <Row k="Products" v={eur(q.total)} />
          <Row k="VAT" v={eur(vat)} />
          <div className="flex justify-between items-baseline border-t border-hair pt-3.5">
            <span className="font-bold">Total</span>
            <span className="text-[30px] font-extrabold tabular-nums">{eur(q.total_with_tax)}</span>
          </div>

          {!ordered && !blocked && q.policy.rules_fired.length > 0 && (
            <div className="bg-warn-bg rounded-xl px-4 py-3 text-[16px] flex flex-col gap-1.5">
              <strong className="text-warn">Please check before ordering</strong>
              {q.policy.rules_fired.map((r) => (
                <span key={r.rule_id + (r.product_id ?? "")}>{r.description}</span>
              ))}
            </div>
          )}

          {blocked && (
            <div className="bg-bad-bg text-bad rounded-xl px-4 py-3 text-[16px] flex flex-col gap-1.5">
              <strong>This can&apos;t be ordered</strong>
              {q.policy.rules_fired.map((r) => (
                <span key={r.rule_id + (r.product_id ?? "")}>{r.description}</span>
              ))}
            </div>
          )}

          {req.status === "WAITING_APPROVAL" && req.approval_id && (
            <button
              onClick={onApprove}
              disabled={busy}
              className="w-full min-h-[60px] rounded-full bg-lime text-petrol-dark text-lg font-extrabold disabled:opacity-60"
            >
              Approve order
            </button>
          )}

          {ordered && req.order && (
            <div className="bg-good-bg rounded-xl px-4 py-3.5 flex flex-col gap-0.5">
              <strong>
                {req.order.approved_by ? `Thanks ${req.order.approved_by.split(" ")[0]}, order placed.` : "Ordered automatically."}
              </strong>
              {!req.order.approved_by && (
                <span className="text-[15px]">Nothing needed your check, and it is within your company's ordering limits.</span>
              )}
              <span className="text-[15px]">Arrives {day(req.order.delivery_date)}.</span>
              <Link href={`/audit/${req.request_id}`} className="text-[15px] text-petrol underline">
                See order details
              </Link>
            </div>
          )}
          <p className="m-0 text-sm text-muted">Prices and stock straight from Valio Aimo.</p>
        </aside>
      </div>
    </div>
  );
}

function Row({ k, v, strong }: { k: string; v: string; strong?: boolean }) {
  return (
    <div className="flex justify-between gap-3 text-base text-soft">
      <span>{k}</span>
      <span className={`tabular-nums text-right ${strong ? "font-semibold text-ink" : ""}`}>{v}</span>
    </div>
  );
}
