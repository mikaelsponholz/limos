"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useCustomer } from "@/lib/customer";
import { readSettings, requestOverrides } from "@/lib/settings";
import { assumedLines } from "@/lib/assumed";
import OrderLine from "@/components/OrderLine";
import Progress from "@/components/Progress";
import RequestBox from "@/components/RequestBox";
import { day, dayTime, eur } from "@/lib/format";
import { ApiError, type LimosRequest, type Step, type Suggestion } from "@/lib/types";

const POLL_MS = 1500;

export default function OrderPage() {
  const { customer, reload } = useCustomer();
  const [text, setText] = useState("");
  const [req, setReq] = useState<LimosRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reorderTitle, setReorderTitle] = useState<string | null>(null);
  // Set while a list is being made: the gateway's steps so far, and the past order tapped (if any).
  const [running, setRunning] = useState<{ steps: Step[]; startedAt: number; reorderOf?: string } | null>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const version = useRef(0);
  const focusOnStart = useRef(false);

  useEffect(() => {
    if (field.current) {
      field.current.style.height = "auto";
      field.current.style.height = `${field.current.scrollHeight}px`;
      if (focusOnStart.current) {
        field.current.focus();
        focusOnStart.current = false;
      }
    }
  }, [text, req, customer]);

  // A new customer starts a fresh page.
  useEffect(() => {
    version.current += 1;
    if (timer.current) clearTimeout(timer.current);
    setReq(null);
    setText("");
    setError(null);
    setRunning(null);
    setBusy(false);
    setReorderTitle(null);
  }, [customer?.customer_id]);

  const poll = useCallback((id: string, activeVersion = version.current) => {
    if (timer.current) clearTimeout(timer.current);
    const tick = async () => {
      try {
        const r = await api.getRequest(id);
        if (activeVersion !== version.current) return;
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
        if (activeVersion !== version.current) return;
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
    const activeVersion = ++version.current;
    setReorderTitle(body.reorder_of ? customer.order_history.find((o) => o.order_ref === body.reorder_of)?.title ?? null : null);
    setError(null);
    setBusy(true);
    setRunning({ steps: [], startedAt: Date.now(), reorderOf: body.reorder_of });
    try {
      const { request_id } = await api.createRequest({
        customer_id: customer.customer_id,
        ...requestOverrides(readSettings(customer.customer_id)),
        ...body,
      });
      if (activeVersion === version.current) poll(request_id, activeVersion);
    } catch {
      if (activeVersion !== version.current) return;
      setBusy(false);
      setRunning(null);
      setError("We could not reach Limos. Please try again.");
    }
  };

  const answer = async (value: string | null) => {
    if (!req?.question) return;
    const activeVersion = version.current;
    setBusy(true);
    setError(null);
    setRunning({ steps: [], startedAt: Date.now() });
    try {
      await api.answer(req.request_id, req.question.question_id, value);
      if (activeVersion === version.current) poll(req.request_id, activeVersion);
    } catch (e) {
      if (activeVersion !== version.current) return;
      setBusy(false);
      setRunning(null);
      setError(e instanceof ApiError ? e.message : "Something went wrong. Please try again.");
    }
  };

  const accept = async (s: Suggestion) => {
    if (!req) return;
    const activeVersion = version.current;
    setBusy(true);
    setError(null);
    setRunning({ steps: [], startedAt: Date.now() });
    try {
      await api.acceptSuggestion(req.request_id, s.suggestion_id);
      if (activeVersion === version.current) poll(req.request_id, activeVersion);
    } catch (e) {
      if (activeVersion !== version.current) return;
      setBusy(false);
      setRunning(null);
      setError(e instanceof ApiError ? e.message : "Something went wrong. Please try again.");
    }
  };

  const approve = async () => {
    if (!req?.quote || !req.approval_id || !customer) return;
    const activeVersion = version.current;
    setBusy(true);
    setError(null);
    try {
      await api.approve(req.approval_id, {
        quote_id: req.quote.quote_id,
        content_hash: req.quote.content_hash,
        approver: customer.name,
      });
      reload();
      if (activeVersion === version.current) poll(req.request_id, activeVersion);
    } catch (e) {
      if (activeVersion !== version.current) return;
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
  const submit = () => {
    if (text.trim()) start({ text: text.trim() });
    else field.current?.focus();
  };
  const requestText = reorderTitle ?? req?.input.text ?? req?.intent?.based_on?.title ?? text;
  const reset = (keepText = false) => {
    version.current += 1;
    if (timer.current) clearTimeout(timer.current);
    setReq(null);
    setText(keepText ? requestText : "");
    setError(null);
    setRunning(null);
    setBusy(false);
    setReorderTitle(null);
    focusOnStart.current = keepText;
    window.scrollTo({ top: 0, behavior: "instant" });
  };

  if (req) {
    return (
      <main className="w-full max-w-[760px] mx-auto px-4 sm:px-6 pt-8 pb-12 flex flex-col gap-5">
        <h1 className="sr-only">Your order</h1>
        <RequestBox text={requestText} onNew={() => reset()} />
        {running && <Progress steps={running.steps} startedAt={running.startedAt} />}
        {(error || req.status === "FAILED") && (
          <p role="alert" className="m-0 bg-bad-bg text-bad rounded-xl px-4 py-3">
            {error ?? req.error?.message ?? "Something went wrong."}
          </p>
        )}
        {req.quote && (
          <ListView req={req} busy={busy} first={first} company={customer.company} onApprove={approve} onAccept={accept} onChange={() => reset(true)} />
        )}
        {req.status === "NEEDS_INPUT" && req.question && (
          <section className={`bg-surface rounded-xl p-5 sm:p-6 flex flex-col gap-4 motion-safe:transition-opacity motion-safe:duration-150 ${busy ? "opacity-60" : ""}`}>
            <h2 className="m-0 text-[24px] font-semibold">{req.question.text}</h2>
            <div className="flex flex-wrap gap-3">
              {req.question.choices.map((c) => (
                <button
                  key={c}
                  type="button"
                  disabled={busy}
                  onClick={() => answer(c)}
                  className="min-h-14 min-w-24 max-w-full break-words px-6 rounded-md border border-petrol text-petrol text-[22px] font-semibold hover:bg-petrol-tint"
                >
                  {c}
                </button>
              ))}
            </div>
            {req.question.default && (
              <button type="button" disabled={busy} onClick={() => answer(null)} className="self-start text-petrol underline min-h-11">
                Use default ({req.question.default})
              </button>
            )}
          </section>
        )}
      </main>
    );
  }

  return (
    <main className="w-full max-w-[680px] mx-auto px-4 sm:px-6 flex flex-col gap-6 flex-1 justify-center py-10 sm:py-16">
      <section className="flex flex-col gap-5">
        <h1 className="m-0 leading-tight font-normal tracking-tight text-[30px] sm:text-[36px] text-center">
          {last ? `Hi ${first}, same as last time?` : `Hi ${first}, what do you need?`}
        </h1>
        <form
          className="bg-surface rounded-xl border border-line focus-within:border-petrol focus-within:ring-4 focus-within:ring-petrol-tint p-3 flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <textarea
            ref={field}
            rows={2}
            aria-label="What do you need?"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                if (!busy) submit();
              }
            }}
            placeholder="What do you need?"
            className="w-full min-h-[61px] max-h-[218px] overflow-y-auto bg-transparent px-1.5 py-1 text-[19px] leading-snug resize-none outline-none placeholder:text-muted"
          />
          <button
            type="submit"
            disabled={busy}
            className="self-end min-h-11 px-3 rounded-md text-petrol text-[17px] font-semibold flex items-center gap-2 enabled:hover:bg-petrol-tint enabled:hover:underline disabled:text-soft"
          >
            {busy ? "Making your list…" : "Make my list"}
            {!busy && (
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M5 12h14M12 5l7 7-7 7" />
              </svg>
            )}
          </button>
        </form>
        {running && <Progress steps={running.steps} startedAt={running.startedAt} />}
        {error && <p role="alert" className="m-0 bg-bad-bg text-bad rounded-xl px-4 py-3">{error}</p>}
        {history.length > 0 && (
          <section aria-labelledby="again-h" className="flex flex-col gap-2 mt-3">
            <h2 id="again-h" className="m-0 text-base font-semibold text-soft">Order again</h2>
            <ul className="m-0 p-0 list-none bg-surface rounded-xl px-4 divide-y divide-hair">
              {history.map((o) => (
                <li key={o.order_ref}>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => start({ reorder_of: o.order_ref })}
                    aria-current={running?.reorderOf === o.order_ref ? "true" : undefined}
                    className="group w-full text-left py-3 min-h-14 flex items-center gap-4"
                  >
                    <span className="flex-1 min-w-0">
                      <span className="block text-[17px] font-medium leading-snug group-hover:text-petrol group-hover:underline">{o.title}</span>
                      <span className="block text-[15px] text-soft">
                        {running?.reorderOf === o.order_ref ? (
                          <span className="text-petrol font-medium">Getting today&apos;s prices…</span>
                        ) : (
                          <>{day(o.date)}{o.guests ? ` | ${o.guests} people` : ""}</>
                        )}
                      </span>
                    </span>
                    {o.total > 0 && <span className="flex-none text-[17px] font-medium tabular-nums">{eur(o.total)}</span>}
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </section>
    </main>
  );
}

function ListView({
  req,
  busy,
  first,
  company,
  onApprove,
  onAccept,
  onChange,
}: {
  req: LimosRequest;
  busy: boolean;
  first: string;
  company: string;
  onApprove: () => void;
  onAccept: (s: Suggestion) => void;
  onChange: () => void;
}) {
  const q = req.quote!;
  const ordered = req.status === "ORDERED";
  const blocked = !ordered && (req.status === "BLOCKED" || q.policy.outcome === "BLOCK");
  const vat = q.total_with_tax - q.total;
  const gaps = assumedLines(req.intent?.filled_gaps ?? []);
  const offered = req.suggestions.filter((s) => s.status === "OFFERED");
  const rules = q.policy.rules_fired;
  const generalRules = rules.filter((r) => !q.lines.some((l) => l.product_id === r.product_id));
  const linesToCheck = q.lines.filter((l) => rules.some((r) => r.product_id === l.product_id)).length;

  return (
    <div className={`flex flex-col gap-5 motion-safe:transition-opacity motion-safe:duration-150 ${busy ? "opacity-60" : ""}`} aria-busy={busy}>
      <section aria-label="Summary" className="bg-petrol text-white rounded-xl p-5 sm:p-6 flex flex-col gap-5">
        {ordered && (
          <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 pb-5 border-b border-white/25">
            <div className="min-w-0">
              <h2 className="m-0 text-[24px] leading-tight font-semibold">Order placed</h2>
              <p className="m-0 mt-1 text-[17px]">
                {req.order?.approved_by ? `Thanks, ${first}. Valio Aimo has your order.` : `Ordered automatically, within ${company}'s ordering limits.`}
              </p>
            </div>
            <Link href={`/audit/${req.request_id}`} className="min-h-11 inline-flex items-center text-[16px] font-medium text-white underline">
              See order details
            </Link>
          </div>
        )}
        {/* Two columns built the same way (label, big value, small line), bottom aligned with the action on the right. */}
        <div className="grid grid-cols-1 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] items-end gap-5 sm:gap-6">
          <div>
            <p className="m-0 text-[15px] text-petrol-tint">Total incl. VAT</p>
            <p className="m-0 text-[40px] leading-[1.15] font-semibold tabular-nums">{eur(q.total_with_tax)}</p>
            <p className="m-0 text-[15px] text-petrol-tint">Products {eur(q.total)} | VAT {eur(vat)}</p>
          </div>
          <div>
            <p className="m-0 text-[15px] text-petrol-tint">{ordered ? "Arrives" : "Delivery"}</p>
            <p className="m-0 text-[40px] leading-[1.15] font-semibold whitespace-nowrap">{day(req.order?.delivery_date ?? q.delivery_date)}</p>
            {!ordered && (
              <p className="m-0 text-[15px] text-petrol-tint">
                {q.order_deadline ? `Order by ${dayTime(q.order_deadline)}` : "No order deadline from Valio"}
              </p>
            )}
          </div>
          {req.status === "WAITING_APPROVAL" && !blocked && req.approval_id && (
            <button type="button" onClick={onApprove} disabled={busy} className="w-full sm:w-auto shrink-0 min-h-14 px-7 rounded-md bg-lime text-petrol-dark text-[18px] font-semibold disabled:opacity-60">
              Approve order
            </button>
          )}
          {blocked && <p className="m-0 text-[18px] font-semibold">Can&apos;t be ordered</p>}
        </div>
      </section>

      {!ordered && generalRules.length === 0 && linesToCheck > 0 && (
        <p className="m-0 flex items-center gap-2.5 text-ink text-[17px]">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden className="shrink-0 text-petrol">
            <path d="m10.3 3.9-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3.1l-8-14a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4m0 4h.01" />
          </svg>
          {linesToCheck} {linesToCheck === 1 ? "line below needs" : "lines below need"} a look before you approve.
        </p>
      )}

      {!ordered && generalRules.length > 0 && (
        <section className="bg-surface rounded-xl px-5 sm:px-6 py-2">
          <h2 className="my-3 text-[20px] font-semibold">{blocked ? "Why it can't be ordered" : "Check before you approve"}</h2>
          <ul className="m-0 p-0 list-none divide-y divide-hair">
            {generalRules.map((r, i) => (
              <li key={`${r.rule_id}-${i}`} className="flex items-start gap-2.5 py-3 text-ink text-[17px]">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden className={`shrink-0 mt-0.5 ${blocked ? "text-bad" : "text-petrol"}`}>
                  <path d="m10.3 3.9-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3.1l-8-14a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4m0 4h.01" />
                </svg>
                <span>{r.description}</span>
              </li>
            ))}
            {linesToCheck > 0 && <li className="py-3 text-soft text-[17px]">{linesToCheck} {linesToCheck === 1 ? "line below needs" : "lines below need"} a look.</li>}
          </ul>
        </section>
      )}

      <section aria-labelledby="list-h" className="bg-surface rounded-xl px-5 sm:px-6 py-2 min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-2 my-3">
          <h2 id="list-h" className="m-0 text-[20px] font-semibold">Your list</h2>
          <span className="text-[16px] text-soft">| {q.lines.length} {q.lines.length === 1 ? "product" : "products"}</span>
        </div>
        <div className="divide-y divide-hair">
          {q.lines.map((l) => <OrderLine key={l.product_id} l={l} rules={rules} />)}
        </div>
      </section>

      {gaps.length > 0 && !ordered && (
        <section className="bg-surface rounded-xl px-5 sm:px-6 py-2">
          <h2 className="my-3 text-[20px] font-semibold">How I read your request</h2>
          <ul className="m-0 p-0 list-none divide-y divide-hair">
            {gaps.map((g, i) => (
              <li key={i} className="flex items-center justify-between gap-4 py-2 text-[17px]">
                <span className="text-ink min-w-0">{g.text.replace(/^We assumed:\s*/i, "")}</span>
                {g.toSettings ? (
                  <Link href="/settings" className="shrink-0 inline-flex items-center min-h-11 text-petrol hover:underline">Change</Link>
                ) : (
                  <button type="button" onClick={onChange} className="shrink-0 min-h-11 text-petrol hover:underline">Change</button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {offered.length > 0 && !ordered && (
        <section className="bg-surface rounded-xl px-5 sm:px-6 py-2">
          <h2 className="my-3 text-[20px] font-semibold">If you want</h2>
          <ul className="m-0 p-0 list-none divide-y divide-hair">
            {offered.map((s) => {
              const replaced = s.replaces_item_id ? q.lines.find((l) => l.item_id === s.replaces_item_id) : null;
              const delta = s.offer ? s.offer.line_total_with_tax - (replaced?.line_total_with_tax ?? 0) : null;
              return (
                <li key={s.suggestion_id} className="py-4 flex flex-wrap items-center justify-between gap-3">
                  <div className="flex-[1_1_280px] min-w-0">
                    <p className="m-0 font-medium">{s.title}</p>
                    <p className="m-0 text-[16px] text-soft">{s.reason}</p>
                  </div>
                  {delta !== null && <span className="font-semibold tabular-nums">{delta < 0 ? "−" : "+"}{eur(Math.abs(delta))}</span>}
                  <button type="button" disabled={busy} onClick={() => onAccept(s)} className="min-h-11 px-5 rounded-md border border-petrol text-petrol font-semibold hover:bg-petrol-tint">
                    {s.replaces_item_id ? "Swap" : "Add"}
                  </button>
                </li>
              );
            })}
          </ul>
        </section>
      )}
    </div>
  );
}
