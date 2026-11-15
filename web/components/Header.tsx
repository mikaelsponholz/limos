"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useCustomer } from "@/lib/customer";

// Lucide "settings" icon.
const GEAR =
  "M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z";

export default function Header() {
  const { customer } = useCustomer();
  const path = usePathname();
  const [pending, setPending] = useState(0);

  useEffect(() => {
    const load = () =>
      api
        .approvals()
        .then((a) => setPending(a.length))
        .catch(() => {});
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [path]);

  return (
    <header className="bg-surface border-b border-hair">
      <div className="max-w-[1080px] mx-auto px-4 sm:px-6 min-h-16 flex items-center justify-between gap-2 sm:gap-4">
        <Link href="/" className="min-h-11 flex items-center font-semibold text-[22px] text-petrol tracking-tight no-underline whitespace-nowrap shrink-0">
          Chef Aimo
        </Link>
        <nav aria-label="Main" className="flex items-center gap-2 sm:gap-4 min-w-0">
          <Link
            href="/approve"
            aria-current={path.startsWith("/approve") ? "page" : undefined}
            className={`min-h-11 text-base no-underline flex items-center gap-1.5 shrink-0 ${
              path.startsWith("/approve") ? "text-petrol font-semibold" : "text-ink font-medium hover:text-petrol"
            }`}
          >
            Approvals
            <span className="text-petrol font-semibold tabular-nums">{pending}</span>
            <span className="sr-only"> waiting</span>
          </Link>
          {customer && (
            <>
              <span aria-hidden className="w-px h-6 bg-hair shrink-0" />
              <div className="flex items-center min-w-0">
                <span className="text-base whitespace-nowrap truncate min-w-0">
                  <span className="font-medium text-ink">
                    <span className="sm:hidden">{customer.name.split(" ")[0]}</span>
                    <span className="hidden sm:inline">{customer.name}</span>
                  </span>
                  <span className="hidden sm:inline text-soft"> | {customer.company}</span>
                </span>
                <Link
                  href="/settings"
                  aria-label="Settings"
                  title="Settings"
                  aria-current={path === "/settings" ? "page" : undefined}
                  className={`flex-none w-11 h-11 rounded-md flex items-center justify-center hover:bg-petrol-tint ${
                    path === "/settings" ? "text-petrol bg-petrol-tint" : "text-petrol"
                  }`}
                >
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                    <path d={GEAR} />
                  </svg>
                </Link>
              </div>
            </>
          )}
        </nav>
      </div>
    </header>
  );
}
