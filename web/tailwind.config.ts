import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        page: "#F6F4EF",
        ink: "#1F2A30",
        muted: "#6B767B",
        soft: "#4A565C",
        line: "#E7E3DA",
        hair: "#F0EDE6",
        petrol: { DEFAULT: "#005670", dark: "#00394B", tint: "#E3EEF1", ring: "#D3E6EB" },
        lime: "#C0E021",
        good: { DEFAULT: "#3F6000", bg: "#EAF4D3" },
        warn: { DEFAULT: "#8A4B00", bg: "#FFF3E0" },
        bad: { DEFAULT: "#9B1C14", bg: "#FDECEA" },
      },
      fontFamily: { sans: ["var(--font-figtree)", "system-ui", "sans-serif"] },
      boxShadow: { card: "0 1px 2px rgba(31,42,48,0.06)" },
    },
  },
  plugins: [],
} satisfies Config;
