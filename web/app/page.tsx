"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useCustomer } from "@/lib/customer";
import { readSettings, requestOverrides } from "@/lib/settings";
import Progress from "@/components/Progress";
import ConversationReply from "@/components/ConversationReply";
import ApprovalBar from "@/components/ApprovalBar";
import { day, eur } from "@/lib/format";
import { ApiError, type LimosRequest, type Offer, type Step, type Suggestion } from "@/lib/types";

const POLL_MS = 1500;
type Turn = { id: string; userText: string; request: LimosRequest | null; previousLines?: Offer[] };

export default function OrderPage() {
  const { customer, reload } = useCustomer();
  const [text, setText] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const latest = turns[turns.length - 1];
  const req = latest?.request ?? null;
  const inConversation = turns.length > 0;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reorderOf, setReorderOf] = useState<string | undefined>();
  // Set while a list is being made: the gateway's steps so far, and the past order tapped (if any).
  const [running, setRunning] = useState<{ steps: Step[]; startedAt: number; reorderOf?: string } | null>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const version = useRef(0);
  const newest = useRef<HTMLElement>(null);
  const bottomBar = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (field.current) {
      field.current.style.height = "auto";
      field.current.style.height = `${field.current.scrollHeight}px`;
    }
  }, [text, inConversation, customer]);

  useEffect(() => {
    const el = newest.current;
    if (!el) return;
    const frame = requestAnimationFrame(() => el.scrollIntoView({
      block: "start",
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
    }));
    return () => cancelAnimationFrame(frame);
  }, [turns.length, req?.status, error]);

  // A new customer starts a fresh page.
  useEffect(() => {
    version.current += 1;
    if (timer.current) clearTimeout(timer.current);
    setTurns([]);
    setText("");
    setError(null);
    setRunning(null);
    setBusy(false);
    setReorderOf(undefined);
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
        setTurns((current) => current.map((turn, i) => i === current.length - 1 ? { ...turn, request: r } : turn));
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
      version.current += 1;
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const start = async (body: { text?: string; reorder_of?: string }, refresh = false) => {
    if (!customer) return;
    const revises = !refresh && inConversation ? req?.request_id : undefined;
    if (!refresh && (busy || (inConversation && (!revises || req?.status === "RUNNING" || req?.status === "ORDERED")))) return;
    const activeVersion = ++version.current;
    if (timer.current) clearTimeout(timer.current);
    const orderRef = body.reorder_of ?? reorderOf;
    const requestText = refresh ? req?.input.text ?? undefined : body.text;
    if (!refresh) {
      const previousLines = [...turns].reverse().find((turn) => turn.request?.quote)?.request?.quote?.lines;
      const title = customer.order_history.find((order) => order.order_ref === body.reorder_of)?.title ?? "past order";
      setTurns((current) => [...current, {
        id: String(activeVersion),
        userText: body.reorder_of ? "Order again: " + title : body.text ?? "",
        request: null,
        previousLines,
      }]);
      setText("");
      setError(null);
    }
    setReorderOf(orderRef);
    setBusy(true);
    setRunning({ steps: [], startedAt: Date.now(), reorderOf: orderRef });
    try {
      const { request_id } = await api.createRequest({
        customer_id: customer.customer_id,
        ...requestOverrides(readSettings(customer.customer_id)),
        text: requestText,
        ...(revises ? { revises } : { reorder_of: orderRef }),
      });
      if (activeVersion === version.current) poll(request_id, activeVersion);
    } catch (e) {
      if (activeVersion !== version.current) return;
      setBusy(false);
      setRunning(null);
      setError(revises && e instanceof ApiError && e.status === 409 && e.code === "NOT_REVISABLE"
        ? "That list can't be changed any more. Start a new request to order something else."
        : revises && e instanceof ApiError && e.status === 404
          ? "I couldn't find the list to change. Start a new request."
          : "We could not reach Limos. Please try again.");
    }
  };

  const answer = async (value: string | null) => {
    if (!req?.question || busy) return;
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
    if (!req || busy) return;
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
    if (!req?.quote || !req.approval_id || !customer || busy) return;
    const activeVersion = version.current;
    setBusy(true);
    setError(null);
    try {
      await api.approve(req.approval_id, {
        quote_id: req.quote.quote_id,
        content_hash: req.quote.content_hash,
        approver: customer.name,
      });
      if (activeVersion !== version.current) return;
      reload();
      if (activeVersion === version.current) poll(req.request_id, activeVersion);
    } catch (e) {
      if (activeVersion !== version.current) return;
      setBusy(false);
      if (e instanceof ApiError && e.status === 409) {
        setError("Prices changed while you were looking. Here is the updated list.");
        poll(req.request_id, activeVersion);
      } else if (e instanceof ApiError && e.status === 410) {
        setError("This list expired. Checking prices again.");
        start({}, true);
      } else setError("The order did not go through. Please try again.");
    }
  };

  if (!customer) return <main className="max-w-[1080px] mx-auto px-4 sm:px-6 py-10 text-muted">Loading…</main>;

  const first = customer.name.split(" ")[0];
  const history = customer.order_history.slice(0, 3);
  const last = history[0];
  const submit = () => {
    if (text.trim() && !busy) start({ text: text.trim() });
    else field.current?.focus();
  };
  const reset = () => {
    version.current += 1;
    if (timer.current) clearTimeout(timer.current);
    setTurns([]);
    setText("");
    setError(null);
    setRunning(null);
    setBusy(false);
    setReorderOf(undefined);
    window.scrollTo({ top: 0, behavior: "instant" });
  };

  if (inConversation) {
    return (
      <main data-chat className="w-full max-w-[720px] mx-auto px-4 sm:px-6 pt-4 flex-1 flex flex-col">
        <h1 className="sr-only">Your order</h1>
        <div className="flex justify-end mb-4">
          <button type="button" onClick={reset} className="min-h-11 text-[16px] font-medium text-petrol hover:underline">New request</button>
        </div>
        <div aria-label="Conversation" className="flex flex-col gap-8 pb-8">
          {turns.map((turn, index) => {
            const isLatest = index === turns.length - 1;
            return (
              <section key={turn.id} ref={isLatest ? newest : undefined} aria-label={"Turn " + (index + 1)} className="flex flex-col gap-5 min-w-0 scroll-mt-4">
                <div className="self-end max-w-[85%] bg-petrol text-white rounded-xl px-4 py-3">
                  <p className="m-0 text-[14px] font-medium text-petrol-tint">You</p>
                  <p className="m-0 text-[17px] whitespace-pre-line break-words [overflow-wrap:anywhere]">{turn.userText}</p>
                </div>
                <div className="self-start w-full bg-surface rounded-xl px-5 sm:px-6 py-4 flex flex-col gap-3 min-w-0">
                  <p className="m-0 text-[14px] font-medium text-petrol">Chef Aimo</p>
                  {isLatest && running ? <Progress steps={running.steps} startedAt={running.startedAt} plain /> : (
                    <>
                      {isLatest && error && <p role="alert" className="m-0 text-[17px] text-bad">{error}</p>}
                      {turn.request && <ConversationReply req={turn.request} previousLines={turn.previousLines} latest={isLatest} busy={busy} onAccept={accept} onAnswer={answer} />}
                    </>
                  )}
                </div>
              </section>
            );
          })}
        </div>
        <div ref={bottomBar} className="sticky bottom-0 z-10 mt-auto bg-page pt-2 sm:pt-3 pb-[max(0.5rem,env(safe-area-inset-bottom))] flex flex-col gap-3">
          {req?.quote && req.status !== "NEEDS_INPUT" && <ApprovalBar req={req} busy={busy} first={first} company={customer.company} onApprove={approve} />}
          {req?.status === "ORDERED" ? (
            <button type="button" onClick={reset} className="self-center min-h-11 px-3 text-[17px] font-medium text-petrol hover:underline">Start a new request</button>
          ) : (
            <form className="bg-surface border border-line rounded-xl focus-within:border-petrol focus-within:ring-4 focus-within:ring-petrol-tint p-2 flex items-end gap-1" onSubmit={(e) => { e.preventDefault(); submit(); }}>
              <textarea
                ref={field}
                rows={1}
                aria-label="Change something or add to the list"
                placeholder="Change something or add to the list"
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    if (!busy) submit();
                  }
                }}
                className="flex-1 min-w-0 w-full min-h-11 max-h-[112px] overflow-y-auto bg-transparent px-1.5 py-2 text-[17px] leading-6 resize-none outline-none placeholder:text-muted"
              />
              <button type="submit" disabled={busy || !text.trim()} className="flex-none min-h-11 px-2 rounded-md text-petrol text-[17px] font-semibold flex items-center gap-1 enabled:hover:bg-petrol-tint enabled:hover:underline disabled:text-soft">
                Send
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M5 12h14M12 5l7 7-7 7" /></svg>
              </button>
            </form>
          )}
          <p className="m-0 text-center text-[14px] text-soft">
            <strong className="font-semibold text-ink">Demo</strong> | Prices and stock from the Valio Aimo test API. Orders are simulated.
          </p>
        </div>
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
