import type { Config } from "tailwindcss";

/**
 * The palette is defined once as CSS variables in globals.css and referenced here, so
 * light and dark are one token set rather than two parallel colour scales that drift.
 */
export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        vellum: "rgb(var(--vellum) / <alpha-value>)",
        raised: "rgb(var(--raised) / <alpha-value>)",
        sunk: "rgb(var(--sunk) / <alpha-value>)",
        ink: "rgb(var(--ink) / <alpha-value>)",
        soft: "rgb(var(--ink-soft) / <alpha-value>)",
        faint: "rgb(var(--ink-faint) / <alpha-value>)",
        rule: "rgb(var(--rule) / <alpha-value>)",
        sepia: "rgb(var(--sepia) / <alpha-value>)",
        gilt: "rgb(var(--gilt) / <alpha-value>)",
        rubric: "rgb(var(--rubric) / <alpha-value>)",
        verdigris: "rgb(var(--verdigris) / <alpha-value>)",
      },
      fontFamily: {
        display: ["var(--font-display)", "Georgia", "serif"],
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      boxShadow: {
        sheet: "0 1px 2px rgb(var(--shadow) / 0.05), 0 12px 32px -16px rgb(var(--shadow) / 0.22)",
        lift: "0 2px 6px rgb(var(--shadow) / 0.08), 0 24px 60px -24px rgb(var(--shadow) / 0.35)",
      },
    },
  },
  plugins: [],
} satisfies Config;
