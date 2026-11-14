"use client";
import { useLayoutEffect, useRef, useState } from "react";

/** The customer's request on the list page: a box showing at most three lines, with "Show all" when it is longer. */
export default function RequestBox({ text, onNew }: { text: string; onNew: () => void }) {
  const [open, setOpen] = useState(false);
  const [long, setLong] = useState(false);
  const body = useRef<HTMLParagraphElement>(null);

  // Measure while clamped: longer than three lines means the toggle is needed.
  useLayoutEffect(() => {
    const el = body.current;
    if (!el || open) return;
    const measure = () => setLong(el.scrollHeight > el.clientHeight + 1);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [text, open]);

  return (
    <section aria-label="Your request" className="bg-surface border border-line rounded-xl px-4 sm:px-5 py-3">
      <div className="flex items-center justify-between gap-4">
        <h2 className="m-0 text-[15px] font-medium text-soft">Your request</h2>
        <button type="button" onClick={onNew} className="shrink-0 min-h-11 -my-2 text-[16px] font-medium text-petrol hover:underline">
          New request
        </button>
      </div>
      <p
        id="request-text"
        ref={body}
        className={`m-0 mt-1 text-[17px] text-ink whitespace-pre-line break-words ${open ? "" : "line-clamp-3"}`}
      >
        {text}
      </p>
      {(long || open) && (
        <button
          type="button"
          aria-expanded={open}
          aria-controls="request-text"
          onClick={() => setOpen((o) => !o)}
          className="mt-1 min-h-11 -mb-2 inline-flex items-center gap-1.5 text-[16px] font-medium text-petrol hover:underline"
        >
          {open ? "Show less" : "Show all"}
          <svg
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden
            className={`motion-safe:transition-transform motion-safe:duration-150 ${open ? "rotate-180" : ""}`}
          >
            <path d="m6 9 6 6 6-6" />
          </svg>
        </button>
      )}
    </section>
  );
}
