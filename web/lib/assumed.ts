// "We assumed" lines from intent.filled_gaps. The gateway's notes for profile and
// default values are raw ("Buyer bistro-example, from Alex Example's profile",
// "Preference organic: prefer"), so those fields are worded here from field and value.
// Other notes (history, answers, mock settings) already read well and are kept.
import { day } from "./format";
import { allergenLabel, listWords, prefLabel } from "./settings";
import type { FilledGap, Preference, Priority } from "./types";

export interface AssumedLine {
  text: string;
  /** "Change" opens Settings (the value came from settings or the profile), not the text field. */
  toSettings: boolean;
}

const PRIORITY_SENTENCE: Record<Priority, string> = {
  price: "lowest price first",
  balanced: "a balance of price and quality",
  quality: "best quality first",
};

const sentence = (s: string) => {
  const t = s.charAt(0).toUpperCase() + s.slice(1);
  return /[.!?]$/.test(t) ? t : `${t}.`;
};

function worded(g: FilledGap): string | null {
  const v = g.value;
  switch (g.field) {
    case "priority":
      return typeof v === "string" && v in PRIORITY_SENTENCE ? PRIORITY_SENTENCE[v as Priority] : null;
    case "budget":
      return typeof v === "number" ? `a budget of ${v} €` : null;
    case "exclude_allergens":
      return Array.isArray(v) && v.length ? `never ${listWords(v.map(String).map(allergenLabel))}` : null;
    case "deliver_by":
      return typeof v === "string" && v ? `delivery by ${day(v.slice(0, 10))}` : null;
    default:
      return null;
  }
}

function preferenceText(prefs: Preference[]): string {
  const always = prefs.filter((p) => p.level === "require").map((p) => prefLabel(p.key));
  const ifPossible = prefs.filter((p) => p.level !== "require").map((p) => prefLabel(p.key));
  const parts: string[] = [];
  if (always.length) parts.push(`always ${listWords(always)}`);
  if (ifPossible.length) parts.push(`${listWords(ifPossible)} if possible`);
  return parts.join("; ");
}

export function assumedLines(gaps: FilledGap[]): AssumedLine[] {
  const lines: AssumedLine[] = [];
  const prefs: Preference[] = [];
  let prefsFromProfile = false;
  let prefsAt = -1;

  for (const g of gaps) {
    // The buyer is the customer's own company; nothing for the user to check.
    if (g.field === "buyer_id") continue;
    const fromProfile = g.source === "profile";

    // One line for all "preferences.<key>" gaps, in place of the first one.
    if (g.field.startsWith("preferences.")) {
      const p = g.value as Partial<Preference> | null;
      prefs.push({ key: p?.key ?? g.field.slice("preferences.".length), level: p?.level === "require" ? "require" : "prefer" });
      prefsFromProfile ||= fromProfile;
      if (prefsAt < 0) {
        prefsAt = lines.length;
        lines.push({ text: "", toSettings: true });
      }
      continue;
    }

    if (g.source === "form") {
      lines.push({ text: g.note, toSettings: true });
      continue;
    }

    const w = worded(g);
    const text = w ? `${w}${fromProfile ? ", from your settings" : ""}` : g.note;
    lines.push({ text: `We assumed: ${sentence(text)}`, toSettings: fromProfile });
  }

  if (prefsAt >= 0) {
    const text = `${preferenceText(prefs)}${prefsFromProfile ? ", from your settings" : ""}`;
    lines[prefsAt] = { text: `We assumed: ${sentence(text)}`, toSettings: true };
  }
  return lines;
}
