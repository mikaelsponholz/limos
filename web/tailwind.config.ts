import type { Config } from "tailwindcss";

// Colours live as CSS variables in app/globals.css (Valio Aimo palette), so a dark theme can be added there.
const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        page: v("page"),
        surface: v("surface"),
        ink: v("ink"),
        soft: v("soft"),
        muted: v("muted"),
        line: v("line"),
        hair: v("hair"),
        petrol: { DEFAULT: v("petrol"), dark: v("petrol-dark"), tint: v("petrol-tint"), ring: v("petrol-ring") },
        lime: v("lime"),
        good: { DEFAULT: v("good"), bg: v("good-bg") },
        warn: { DEFAULT: v("warn"), bg: v("warn-bg") },
        bad: { DEFAULT: v("bad"), bg: v("bad-bg") },
      },
      fontFamily: { sans: ["var(--font-outfit)", "system-ui", "sans-serif"] },
      boxShadow: { card: "0 1px 2px rgb(var(--ink) / 0.06)" },
    },
  },
  plugins: [],
} satisfies Config;
