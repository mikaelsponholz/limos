"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { initials, useCustomer } from "@/lib/customer";

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
    <header className="bg-white border-b border-line">
      <div className="max-w-[1080px] mx-auto px-4 sm:px-6 min-h-16 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 py-2">
        <div className="flex items-center gap-5">
          <Link href="/" className="min-h-11 flex items-center font-extrabold text-2xl text-petrol tracking-tight no-underline">
            limos
          </Link>
          <Link
            href="/approve"
            className={`min-h-11 text-base font-semibold no-underline flex items-center gap-1.5 ${path.startsWith("/approve") ? "text-petrol" : "text-soft"}`}
          >
            Approvals
            {pending > 0 && (
              <span className="bg-lime text-petrol-dark rounded-full min-w-6 h-6 px-1.5 text-[14px] font-bold flex items-center justify-center">
                {pending}
              </span>
            )}
          </Link>
        </div>
        {customer && (
          <Link
            href="/settings"
            className={`flex items-center gap-2.5 no-underline rounded-full pr-2 min-w-0 ${path === "/settings" ? "text-petrol" : "text-soft"}`}
          >
            <span
              aria-hidden
              className="flex-none w-9 h-9 rounded-full bg-petrol-tint text-petrol font-bold text-[15px] flex items-center justify-center"
            >
              {initials(customer)}
            </span>
            <span className="flex flex-col leading-tight min-w-0">
              <span className="text-base text-ink font-semibold truncate">
                {customer.name} · {customer.company}
              </span>
              <span className="text-[14px] underline">Settings</span>
            </span>
          </Link>
        )}
      </div>
    </header>
  );
}
