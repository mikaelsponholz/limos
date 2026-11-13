const TZ = "Europe/Helsinki";

export const eur = (n: number) => `${n.toFixed(2)} €`;

/** "Fri 20 Nov" from "2026-11-20" (date only, no timezone shift). */
export function day(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00Z` : iso);
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: TZ });
}

/** "Thu 19 Nov, 17:00" from an ISO timestamp. */
export function dayTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  const t = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: TZ });
  return `${day(iso)}, ${t}`;
}

export const isPast = (iso: string | null | undefined) => !!iso && new Date(iso).getTime() < Date.now();

/** A soft category tile for a product, from words in its name. */
export function categoryOf(name: string): { icon: string; tile: string; ink: string } {
  const n = name.toLowerCase();
  const pick = (icon: string, tile: string, ink: string) => ({ icon, tile, ink });
  if (/cake|bun|bread|rye|pastry|bake/.test(n)) return pick(ICONS.bread, "#FBEEDC", "#9A5B12");
  if (/berr|fruit|apple|lingon|straw|rasp/.test(n)) return pick(ICONS.berry, "#FBE3E8", "#A1213F");
  if (/cucumber|salad|tomato|potato|veg|greens|onion/.test(n)) return pick(ICONS.leaf, "#E6F2DA", "#3F6000");
  if (/milk|cream|butter|yog|quark|cheese|dairy/.test(n)) return pick(ICONS.carton, "#E3EEF1", "#005670");
  return pick(ICONS.box, "#EEEBE4", "#4A565C");
}

export const ICONS = {
  bread: "M4 20h16M5 20v-7h14v7M5 13c0-2 3-3 7-3s7 1 7 3M12 10V7",
  berry: "M12 7c-4 0-6 3-6 6 0 4 3 8 6 8s6-4 6-8c0-3-2-6-6-6zM12 7V4M9 4.5l3 2.5 3-2.5",
  leaf: "M5 19c0-8 6-14 14-14 0 8-6 14-14 14zM5 19l7-7",
  carton: "M7 21V8l2-4h6l2 4v13zM7 8h10M10 13h4",
  box: "M4 8l8-4 8 4v8l-8 4-8-4zM4 8l8 4 8-4M12 12v8",
  truck: "M3 7h11v9H3zM14 10h4l3 3v3h-7M7 19.3a1.8 1.8 0 1 0 0-3.6 1.8 1.8 0 0 0 0 3.6zM17 19.3a1.8 1.8 0 1 0 0-3.6 1.8 1.8 0 0 0 0 3.6z",
};
