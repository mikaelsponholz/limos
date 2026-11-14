"use client";
import { useState } from "react";
import { useCustomer } from "@/lib/customer";
import { ALLERGENS, EMPTY, PREFERENCES, alwaysAsks, effective, useSettings, type TextSize } from "@/lib/settings";
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
  // Only the preferences in use are shown; the rest open on request, so the page stays short.
  const [allPrefs, setAllPrefs] = useState(false);

  if (!customer) return <main className="w-full min-w-0 max-w-[720px] mx-auto px-4 sm:px-6 py-10 text-[16px] text-soft">Loading…</main>;

  const eff = effective(settings, customer);
  const approvalRequired = alwaysAsks(customer.buyer_id);
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
    <main className="w-full min-w-0 max-w-[720px] mx-auto px-4 sm:px-6 pt-8 pb-12 flex flex-col gap-6">
      <div className="flex items-end justify-between gap-3">
        <h1 className="text-[30px] font-semibold tracking-tight m-0">Settings</h1>
        <span aria-live="polite" className={`text-[15px] font-semibold text-good transition-opacity duration-150 motion-reduce:transition-none ${saved ? "opacity-100" : "opacity-0"}`}>
          Saved
        </span>
      </div>

      <Card title="Your rules" help="Chef Aimo always follows these. They are checked by fixed rules, not by AI.">
        <div className="divide-y divide-hair">
          <Row label="Never include" help="Products that contain or may contain these are left out." stacked>
            <div className="flex flex-wrap gap-2">
              {ALLERGENS.map((a) => {
                const on = eff.exclude_allergens.includes(a.key);
                return (
                  <button
                    key={a.key}
                    type="button"
                    aria-pressed={on}
                    onClick={() => toggleAllergen(a.key)}
                    className={`inline-flex items-center gap-2 min-h-11 px-4 rounded-[4px] text-[16px] font-medium border ${on ? "bg-petrol border-petrol text-white" : "bg-surface border-line text-ink hover:bg-petrol-tint"}`}
                  >
                    {on && (
                      <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="m5 12 4 4L19 6" />
                      </svg>
                    )}
                    {a.label}
                  </button>
                );
              })}
            </div>
          </Row>
          <Row label={<label htmlFor="order-budget">Spending limit per order</label>} help="Above this, Chef Aimo asks you before ordering.">
            <div className="flex items-center gap-2 text-[17px]">
              <input
                id="order-budget"
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
                className="w-40 px-3.5 py-3 rounded-md border border-line bg-surface text-[17px] placeholder:text-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-petrol focus-visible:outline-offset-2"
              />
              €
            </div>
          </Row>
          <Row
            label="Always ask before ordering"
            help={approvalRequired
              ? `Set by ${customer.company}. Every order waits for your tap.`
              : `Set by ${customer.company}. Orders within the company's limits go through without asking.`}
          >
            <span className={`text-[17px] font-semibold ${approvalRequired ? "text-petrol" : "text-ink"}`}>
              {approvalRequired ? "On" : "Off"}
            </span>
          </Row>
        </div>
      </Card>

      <Card title="What you like" help="Used when Chef Aimo picks products. You can always say something else in your message.">
        <div className="divide-y divide-hair">
          <Row label="When we pick products" stacked>
            <Segmented<Priority> label="When we pick products" value={eff.priority} options={PRIORITIES} onChange={(v) => save({ priority: v })} />
            <p className="m-0 text-[15px] text-soft">{PRIORITIES.find((p) => p.key === eff.priority)?.help}</p>
          </Row>
          {PREFERENCES.filter((p) => allPrefs || levelOf(p.key) !== "off").map((p) => (
            <Row key={p.key} label={p.label} help={levelOf(p.key) === "require" ? "Chef Aimo asks you before ordering anything that doesn't match." : undefined}>
              <Segmented label={p.label} value={levelOf(p.key)} options={LEVELS} onChange={(v) => setLevel(p.key, v)} />
            </Row>
          ))}
          <div className="py-3">
            <button type="button" aria-expanded={allPrefs} onClick={() => setAllPrefs((o) => !o)} className="min-h-11 text-[17px] font-medium text-petrol hover:underline">
              {allPrefs ? "Show only what I use" : "Add a preference, like lactose-free or organic"}
            </button>
          </div>
        </div>
      </Card>

      <Card title="Display">
        <Row label="Text size">
          <Segmented<TextSize>
            label="Text size"
            value={settings.textSize}
            options={[
              { key: "normal", label: "Normal" },
              { key: "large", label: "Large" },
            ]}
            onChange={(v) => save({ textSize: v })}
          />
        </Row>
      </Card>

      {changed && (
        <button
          type="button"
          onClick={() => save({ ...EMPTY, textSize: settings.textSize })}
          className="self-start text-petrol underline font-semibold min-h-11 rounded-md"
        >
          Go back to my profile settings
        </button>
      )}

      <Card title="Demo: who is ordering" help="There is no sign-in in this demo. Pick a customer to see their view.">
        <div className="divide-y divide-hair">
          {customers.map((c) => {
            const on = c.customer_id === customer.customer_id;
            return (
              <button
                key={c.customer_id}
                type="button"
                aria-pressed={on}
                onClick={() => choose(c.customer_id)}
                className="w-full text-left py-4 flex items-center justify-between gap-3"
              >
                <span className="min-w-0">
                  <span className="block text-[17px] font-medium">{c.name} | {c.company}</span>
                  <span className="block text-[15px] text-soft">{c.role}</span>
                </span>
                {on && <span className="text-[16px] text-petrol font-semibold shrink-0">Selected</span>}
              </button>
            );
          })}
        </div>
      </Card>
    </main>
  );
}

function Card({ title, help, children }: { title: string; help?: string; children: React.ReactNode }) {
  return (
    <section className="bg-surface rounded-xl px-5 sm:px-6 py-5 flex flex-col gap-3">
      <div>
        <h2 className="m-0 text-[20px] font-semibold">{title}</h2>
        {help && <p className="m-0 text-[16px] text-soft">{help}</p>}
      </div>
      {children}
    </section>
  );
}

function Row({ label, help, stacked, children }: { label: React.ReactNode; help?: string; stacked?: boolean; children: React.ReactNode }) {
  return (
    <div className={`flex flex-col gap-3 py-4 ${stacked ? "" : "sm:flex-row sm:items-center sm:justify-between sm:gap-6"}`}>
      <div className="min-w-0">
        <div className="text-[17px] font-medium">{label}</div>
        {help && <p className="m-0 text-[15px] text-soft">{help}</p>}
      </div>
      {stacked ? children : <div className="min-w-0 sm:shrink-0">{children}</div>}
    </div>
  );
}

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { key: T; label: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="self-start inline-flex max-w-full border border-line rounded-md p-0.5">
      {options.map((o, index) => {
        const on = o.key === value;
        return (
          <button
            key={o.key}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={on ? 0 : -1}
            onClick={() => onChange(o.key)}
            onKeyDown={(event) => {
              let next = index;
              if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % options.length;
              else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index + options.length - 1) % options.length;
              else if (event.key === "Home") next = 0;
              else if (event.key === "End") next = options.length - 1;
              else return;
              event.preventDefault();
              onChange(options[next].key);
              (event.currentTarget.parentElement?.children[next] as HTMLButtonElement | undefined)?.focus();
            }}
            className={`inline-flex items-center justify-center min-w-0 rounded-[4px] px-3.5 min-h-10 text-[16px] ${on ? "bg-petrol text-white font-medium" : "text-ink hover:bg-petrol-tint"}`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
