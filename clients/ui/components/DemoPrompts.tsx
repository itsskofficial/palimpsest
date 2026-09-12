"use client";

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { api, type DemoState } from "@/lib/api";

/**
 * The four things worth trying, shown only inside `palimpsest demo`.
 *
 * A visitor who has not read the sample notes cannot invent a claim that lands on
 * `contradicts` on purpose. Without these the demo shows that *something* happens
 * without showing the thing worth seeing — which relation it picks, and why that is the
 * right one. Each chip carries its own explanation for the same reason: the interesting
 * part is the reasoning, and a result with no stated expectation is just output.
 *
 * Renders nothing at all outside the demo, so this costs a real installation one request
 * and no pixels.
 */
export function DemoPrompts({ onPick }: { onPick: (text: string) => void }) {
  const [state, setState] = useState<DemoState | null>(null);
  const [open, setOpen] = useState<number | null>(null);

  useEffect(() => {
    // A failure here means "not a demo", which is the common case and not worth saying.
    api.demo().then(setState).catch(() => setState(null));
  }, []);

  if (!state?.demo || !state.prompts.length) return null;

  return (
    <section className="mt-4">
      <p className="font-mono text-[11px] uppercase tracking-wider text-faint">
        Try one of these
      </p>

      <div className="mt-2 flex flex-wrap gap-2">
        {state.prompts.map((prompt, i) => (
          <button
            key={prompt.label}
            onClick={() => setOpen(open === i ? null : i)}
            className={`rounded-full border px-3 py-1.5 text-left text-sm transition ${
              open === i
                ? "border-sepia bg-sepia/10 text-ink"
                : "border-rule text-soft hover:border-sepia hover:text-ink"
            }`}
          >
            {prompt.label}
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        {open !== null && (
          <motion.div
            key={open}
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.18 }}
            className="mt-3 rounded-xl border border-rule bg-raised/60 p-4"
          >
            <p className="text-[15px] leading-relaxed text-ink">
              {state.prompts[open].text}
            </p>
            <p className="mt-2 text-[13px] leading-relaxed text-soft">
              {state.prompts[open].why}
            </p>
            <button
              onClick={() => {
                onPick(state.prompts[open!].text);
                setOpen(null);
              }}
              // The house primary button. `bg-ink` was invisible: `--ink` is the *text*
              // colour, which inverts with the theme, so in dark mode this was cream on
              // cream. Semantic tokens are only a help if you use the semantic one.
              className="mt-3 rounded-lg bg-sepia px-4 py-1.5 text-sm font-medium text-vellum transition hover:opacity-90"
            >
              Put it in the box
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      <p className="mt-3 font-mono text-[11px] text-faint">
        demo vault · nothing here touches your Notion
      </p>
    </section>
  );
}
