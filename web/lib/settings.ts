"use client";
// Per-customer settings, kept in this browser. Order settings are sent with every
// POST /requests (fields set on the request win over the profile, contract v0.3).
// null means "use what the profile says".
import { useCallback, useEffect, useState } from "react";
import type { Customer, Preference, Priority } from "./types";

export type TextSize = "normal" | "large";

export interface Settings {
  preferences: Preference[] | null;
  exclude_allergens: string[] | null;
  priority: Priority | null;
  budget: number | null;
  textSize: TextSize;
}

export const EMPTY: Settings = { preferences: null, exclude_allergens: null, priority: null, budget: null, textSize: "normal" };

export const PREFERENCES: { key: string; label: string }[] = [
  { key: "lactose_free", label: "Lactose-free" },
  { key: "gluten_free", label: "Gluten-free" },
  { key: "finnish_origin", label: "Finnish" },
  { key: "organic", label: "Organic" },
  { key: "vegetarian", label: "Vegetarian" },
  { key: "vegan", label: "Vegan" },
  { key: "low_carbon", label: "Low carbon" },
];

export const ALLERGENS: { key: string; label: string }[] = [
  { key: "milk", label: "Milk" },
  { key: "gluten", label: "Gluten" },
  { key: "egg", label: "Egg" },
  { key: "fish", label: "Fish" },
  { key: "crustaceans", label: "Shellfish" },
  { key: "peanuts", label: "Peanuts" },
  { key: "tree_nuts", label: "Nuts" },
  { key: "soy", label: "Soy" },
  { key: "sesame", label: "Sesame" },
  { key: "celery", label: "Celery" },
  { key: "mustard", label: "Mustard" },
];

// Plain words for settings, shared by the mock rules and the "We assumed" lines.
export const allergenLabel = (k: string) => (ALLERGENS.find((a) => a.key === k)?.label ?? k.replace(/_/g, " ")).toLowerCase();
export const prefLabel = (k: string) => {
  const label = PREFERENCES.find((p) => p.key === k)?.label ?? k.replace(/_/g, " ");
  return label === "Finnish" ? label : label.toLowerCase();
};
export const listWords = (xs: string[]) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);
export const PRIORITY_WORDS: Record<Priority, string> = { price: "lowest price", balanced: "balanced", quality: "best quality" };

const key = (customerId: string) => `limos.settings.${customerId}`;

export function readSettings(customerId: string | undefined): Settings {
  if (!customerId) return EMPTY;
  try {
    const raw = localStorage.getItem(key(customerId));
    return raw ? { ...EMPTY, ...JSON.parse(raw) } : EMPTY;
  } catch {
    return EMPTY;
  }
}

export function applyTextSize(size: TextSize) {
  // Most sizes are in px, so zoom scales the whole page evenly.
  document.documentElement.style.setProperty("zoom", size === "large" ? "1.15" : "1");
}

/** Effective values: the saved override, else the customer's profile. */
export function effective(s: Settings, c: Customer | null) {
  return {
    preferences: s.preferences ?? c?.preferences ?? [],
    exclude_allergens: s.exclude_allergens ?? c?.exclude_allergens ?? [],
    priority: s.priority ?? c?.defaults?.priority ?? "balanced",
    budget: s.budget ?? c?.defaults?.budget ?? null,
  };
}

/** Only the fields the user changed, for POST /requests. */
export function requestOverrides(s: Settings) {
  const out: Record<string, unknown> = {};
  if (s.preferences) out.preferences = s.preferences;
  if (s.exclude_allergens) out.exclude_allergens = s.exclude_allergens;
  if (s.priority) out.priority = s.priority;
  if (s.budget !== null) out.budget = s.budget;
  return out;
}

export function useSettings(customerId: string | undefined) {
  const [settings, setSettings] = useState<Settings>(EMPTY);
  useEffect(() => setSettings(readSettings(customerId)), [customerId]);

  const update = useCallback(
    (patch: Partial<Settings>) => {
      setSettings((prev) => {
        const next = { ...prev, ...patch };
        if (customerId) {
          try {
            localStorage.setItem(key(customerId), JSON.stringify(next));
          } catch {
            /* private mode: works for this visit only */
          }
        }
        if (patch.textSize) applyTextSize(patch.textSize);
        return next;
      });
    },
    [customerId],
  );

  return { settings, update };
}
