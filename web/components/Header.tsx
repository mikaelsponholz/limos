"use client";
import Link from "next/link";
import { initials, useCustomer } from "@/lib/customer";

export default function Header() {
  const { customers, customer, choose } = useCustomer();
  return (
    <header className="bg-white border-b border-line">
      <div className="max-w-[1080px] mx-auto px-6 min-h-16 flex items-center justify-between gap-4">
        <Link href="/" className="font-extrabold text-2xl text-petrol tracking-tight no-underline">
          limos
        </Link>
        {customer && (
          <label className="flex items-center gap-3 text-base text-soft">
            <span
              aria-hidden
              className="w-9 h-9 rounded-full bg-petrol-tint text-petrol font-bold text-[15px] flex items-center justify-center"
            >
              {initials(customer)}
            </span>
            <span className="sr-only">Ordering as</span>
            <select
              value={customer.customer_id}
              onChange={(e) => choose(e.target.value)}
              className="bg-transparent border-0 text-base text-soft cursor-pointer pr-1 max-w-[220px]"
            >
              {customers.map((c) => (
                <option key={c.customer_id} value={c.customer_id}>
                  {c.name} · {c.company}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
    </header>
  );
}
