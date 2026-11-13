"use client";
// "Who is ordering": no sign-in, the customer picker is how the user is recognised.
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "./api";
import type { Customer, CustomerSummary } from "./types";

const KEY = "limos.customer";

type Ctx = {
  customers: CustomerSummary[];
  customer: Customer | null;
  choose: (id: string) => void;
  reload: () => void;
};

const CustomerContext = createContext<Ctx>({ customers: [], customer: null, choose: () => {}, reload: () => {} });

function stored(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function CustomerProvider({ children }: { children: React.ReactNode }) {
  const [customers, setCustomers] = useState<CustomerSummary[]>([]);
  const [id, setId] = useState<string | null>(null);
  const [customer, setCustomer] = useState<Customer | null>(null);

  useEffect(() => {
    api.customers().then((list) => {
      setCustomers(list);
      const keep = stored();
      setId(list.some((c) => c.customer_id === keep) ? keep : list[0]?.customer_id ?? null);
    });
  }, []);

  const load = useCallback(() => {
    if (id) api.customer(id).then(setCustomer);
  }, [id]);
  useEffect(load, [load]);

  const choose = (next: string) => {
    try {
      localStorage.setItem(KEY, next);
    } catch {
      /* private mode */
    }
    setCustomer(null);
    setId(next);
  };

  return <CustomerContext.Provider value={{ customers, customer, choose, reload: load }}>{children}</CustomerContext.Provider>;
}

export const useCustomer = () => useContext(CustomerContext);

export const initials = (c: { name: string }) =>
  c.name
    .split(/\s+/)
    .map((p) => p[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
