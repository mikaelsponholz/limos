"use client";
import Link from "next/link";
import { useState } from "react";
import { initials, useCustomer } from "@/lib/customer";
import { day } from "@/lib/format";
import { ALLERGENS, EMPTY, PREFERENCES, effective, useSettings, type TextSize } from "@/lib/settings";
import type { Preference, PreferenceLevel, Priority } from "@/lib/types";

const PRIORITIES: { key: Priority; label: string; help: string }[] = [
  { key: "price", label: "Lowest price", help: "The cheapest product that fits." },
  { key: "balanced", label: "Balanced", help: "Good value and good quality." },
  { key: "quality", label: "Best quality", help: "The best product, even if it costs more." },
];

const LEVELS: { key: PreferenceLevel | "off"; label: string }[] = [
  { key: "off", label: "No" },
  { key: "prefer", label: "If possible" },
  { key: "require", label: "Always" },
];

export default function SettingsPage() {
  const { customer, customers, choose } = useCustomer();
  const { settings, update } = useSettings(customer?.customer_id);
  const [saved, setSaved] = useState(false);

  if (!customer) return <main className="max-w-[720px] mx-auto px-6 py-10 text-muted">Loading…</main>;

  const eff = effective(settings, customer);
  const changed = settings.preferences || settings.exclude_allergens || settings.priority || settings.budget !== null;

  const save = (patch: Parameters<typeof update>[0]) => {
    update(patch);
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  };

  const levelOf = (k: string): PreferenceLevel | "off" => eff.preferences.find((p) => p.key === k)?.level ?? "off";
  const setLevel = (k: string, level: PreferenceLevel | "off") => {
    const rest = eff.preferences.filter((p) => p.key !== k);
    const next: Preference[] = level === "off" ? rest : [...rest, { key: k, level }];
    save({ preferences: next });
  };
  const toggleAllergen = (k: string) => {
    const on = eff.exclude_allergens.includes(k);
    save({ exclude_allergens: on ? eff.exclude_allergens.filter((a) => a !== k) : [...eff.exclude_allergens, k] });
  };

  return (
    <main className="max-w-[720px] mx-auto px-4 sm:px-6 pt-8 pb-12 flex flex-col gap-6">
      <div className="flex items-end justify-between gap-3">
        <h1 className="text-[30px] font-bold tracking-tight m-0">Settings</h1>
        <span aria-live="polite" className={`text-[15px] font-semibold text-good transition-opacity ${saved ? "opacity-100" : "opacity-0"}`}>
          Saved
        </span>
      </div>

      <Card title="You">
        <div className="flex items-center gap-4">
          <span className="flex-none w-14 h-14 rounded-full bg-petrol-tint text-petrol font-bold text-xl flex items-center justify-center">
            {initials(customer)}
          </span>
          <span className="flex flex-col">
            <strong className="text-xl">{customer.name}</strong>
            <span className="text-soft">
              {customer.company} · {customer.role}
            </span>
          </span>
        </div>
        {customer.order_history[0] && (
          <p className="m-0 text-[15px] text-muted">
            Last order: {customer.order_history[0].title}, {day(customer.order_history[0].date)}.
          </p>
        )}
      </Card>

      <Card title="Text size" help="Make everything on the screen bigger.">
        <Segmented<TextSize>
          value={settings.textSize}
          options={[
            { key: "normal", label: "Normal" },
            { key: "large", label: "Large" },
          ]}
          onChange={(v) => save({ textSize: v })}
        />
      </Card>

      <Card title="When we pick products" help="Used for every new list. You can still say something else in your message.">
        <div className="flex flex-col gap-2">
          {PRIORITIES.map((p) => {
            const on = eff.priority === p.key;
            return (
              <button
                key={p.key}
                type="button"
                aria-pressed={on}
                onClick={() => save({ priority: p.key })}
                className={`text-left rounded-xl px-4 py-3 flex gap-3 items-start bg-white ${on ? "ring-2 ring-petrol" : "border border-line"}`}
              >
                <span className={`mt-1 w-5 h-5 rounded-full border-2 flex items-center justify-center ${on ? "border-petrol" : "border-muted"}`}>
                  {on && <span className="w-2.5 h-2.5 rounded-full bg-petrol" />}
                </span>
                <span>
                  <span className="block font-semibold">{p.label}</span>
                  <span className="block text-[15px] text-muted">{p.help}</span>
                </span>
              </button>
            );
          })}
        </div>
      </Card>

      <Card title="What you like" help="“Always” means we ask you before ordering anything that does not match.">
        <ul className="m-0 p-0 list-none flex flex-col">
          {PREFERENCES.map((p) => (
            <li key={p.key} className="flex flex-wrap items-center justify-between gap-2 py-2.5 border-t border-hair first:border-t-0">
              <span className="font-semibold">{p.label}</span>
              <Segmented value={levelOf(p.key)} options={LEVELS} onChange={(v) => setLevel(p.key, v)} small />
            </li>
          ))}
        </ul>
      </Card>

      <Card title="Never include" help="Products that contain or may contain these are left out.">
        <div className="flex flex-wrap gap-2">
          {ALLERGENS.map((a) => {
            const on = eff.exclude_allergens.includes(a.key);
            return (
              <button
                key={a.key}
                type="button"
                aria-pressed={on}
                onClick={() => toggleAllergen(a.key)}
                className={`min-h-11 px-4 rounded-full font-semibold border-2 ${on ? "bg-petrol border-petrol text-white" : "bg-white border-line text-ink"}`}
              >
                {on ? "✓ " : ""}
                {a.label}
              </button>
            );
          })}
        </div>
      </Card>

      <Card title="Budget per order" help="We ask you before ordering anything over this. Leave empty for no limit.">
        <label className="flex items-center gap-2 text-lg">
          <input
            type="number"
            inputMode="decimal"
            min={0}
            placeholder="No limit"
            defaultValue={eff.budget ?? ""}
            key={customer.customer_id + String(eff.budget)}
            onBlur={(e) => {
              const v = e.target.value.trim();
              save({ budget: v === "" ? null : Math.max(0, Number(v)) });
            }}
            className="w-40 px-3.5 py-3 rounded-xl border-2 border-line bg-white text-lg"
          />
          €
        </label>
      </Card>

      <Card title="Ideas and seasonal tips">
        <p className="m-0">
          {customer.playful
            ? "On. We show a few optional ideas under your list, like seasonal swaps."
            : "Off. Your lists show only what you asked for."}
        </p>
      </Card>

      {changed && (
        <button
          type="button"
          onClick={() => save({ ...EMPTY, textSize: settings.textSize })}
          className="self-start text-petrol underline font-semibold min-h-11"
        >
          Go back to my profile settings
        </button>
      )}

      <Card title="Demo: who is ordering" help="There is no sign-in in this demo. Pick a customer to see their view.">
        <div className="flex flex-col gap-2">
          {customers.map((c) => {
            const on = c.customer_id === customer.customer_id;
            return (
              <button
                key={c.customer_id}
                type="button"
                aria-pressed={on}
                onClick={() => choose(c.customer_id)}
                className={`text-left rounded-xl px-4 py-3 bg-white ${on ? "ring-2 ring-petrol" : "border border-line"}`}
              >
                <span className="block font-semibold">
                  {c.name} · {c.company}
                </span>
                <span className="block text-[15px] text-muted">{c.role}</span>
              </button>
            );
          })}
        </div>
      </Card>

      <Link href="/" className="self-start min-h-12 px-6 rounded-full bg-petrol text-white font-bold flex items-center no-underline">
        Back to ordering
      </Link>
    </main>
  );
}

function Card({ title, help, children }: { title: string; help?: string; children: React.ReactNode }) {
  return (
    <section className="bg-white rounded-2xl shadow-card px-5 py-5 flex flex-col gap-3">
      <div>
        <h2 className="m-0 text-xl font-bold">{title}</h2>
        {help && <p className="m-0 text-[15px] text-muted">{help}</p>}
      </div>
      {children}
    </section>
  );
}

function Segmented<T extends string>({
  value,
  options,
  onChange,
  small,
}: {
  value: T;
  options: { key: T; label: string }[];
  onChange: (v: T) => void;
  small?: boolean;
}) {
  return (
    <div role="group" className="self-start inline-flex rounded-full border-2 border-line bg-page p-1 gap-1">
      {options.map((o) => {
        const on = o.key === value;
        return (
          <button
            key={o.key}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(o.key)}
            className={`rounded-full font-semibold ${small ? "min-h-10 px-3.5 text-[16px]" : "min-h-11 px-5"} ${on ? "bg-petrol text-white" : "text-soft"}`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
