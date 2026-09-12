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
        // The inset highlight comes first so it lands *inside* the top edge. On a light
        // ground `--edge-alpha` is 0 and it costs nothing; on a dark one it is the only
        // thing separating a card from the page.
        sheet: [
          "inset 0 1px 0 rgb(var(--edge) / var(--edge-alpha))",
          "0 1px 2px rgb(var(--shadow) / 0.05)",
          "0 12px 32px -16px rgb(var(--shadow) / 0.30)",
        ].join(", "),
        lift: [
          "inset 0 1px 0 rgb(var(--edge) / calc(var(--edge-alpha) * 1.6))",
          "0 2px 6px rgb(var(--shadow) / 0.08)",
          "0 28px 64px -24px rgb(var(--shadow) / 0.45)",
        ].join(", "),
        // A focused capture box: the page leaning in, rather than a browser outline.
        focus: [
          "inset 0 1px 0 rgb(var(--edge) / calc(var(--edge-alpha) * 2))",
          "0 0 0 1px rgb(var(--gilt) / 0.35)",
          "0 0 40px -8px rgb(var(--gilt) / 0.25)",
          "0 20px 50px -24px rgb(var(--shadow) / 0.5)",
        ].join(", "),
      },
      keyframes: {
        // Ink settling into the page as something lands.
        bleed: {
          "0%": { opacity: "0", transform: "translateY(-2px) scale(0.995)" },
          "100%": { opacity: "1", transform: "none" },
        },
        // A slow sweep along a card while the pipeline is still thinking about it.
        scribe: {
          "0%": { backgroundPosition: "-160% 0" },
          "100%": { backgroundPosition: "260% 0" },
        },
      },
      animation: {
        bleed: "bleed 420ms cubic-bezier(0.22, 1, 0.36, 1) both",
        scribe: "scribe 2.4s cubic-bezier(0.4, 0, 0.2, 1) infinite",
      },
    },
  },
  plugins: [],
} satisfies Config;
