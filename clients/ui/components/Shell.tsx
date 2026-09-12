"use client";

import { AnimatePresence, motion } from "motion/react";
import type { SetupState } from "@/lib/api";

export type Tab = "capture" | "activity" | "ask" | "settings";

const TABS: { id: Tab; label: string }[] = [
  { id: "capture", label: "Capture" },
  { id: "activity", label: "Activity" },
  { id: "ask", label: "Ask" },
  { id: "settings", label: "Settings" },
];

export function Shell({
  tab,
  onTab,
  setup,
  children,
}: {
  tab: Tab;
  onTab: (t: Tab) => void;
  setup: SetupState;
  children: React.ReactNode;
}) {
  return (
    <div className="relative z-10 mx-auto min-h-screen max-w-3xl px-6 pb-24 pt-8">
      <motion.header
        initial={{ opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
        className="mb-10 flex items-center justify-between"
      >
        <div className="flex items-baseline gap-3">
          {/*
            The wordmark carries a faint gradient rather than a flat fill — the way ink
            sits heavier where the nib started. Two stops, nothing clever.
          */}
          <h1 className="bg-gradient-to-br from-ink via-ink to-sepia bg-clip-text font-display text-2xl font-semibold tracking-tight text-transparent">
            palimpsest
          </h1>
          <ModePill apply={setup.apply} autonomy={setup.autonomy} />
        </div>

        <nav className="flex items-center gap-1 rounded-full border border-rule bg-raised/70 p-1 shadow-sheet backdrop-blur-sm">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => onTab(t.id)}
              className="relative rounded-full px-3.5 py-1.5 text-sm font-medium transition-colors"
              aria-current={tab === t.id ? "page" : undefined}
            >
              {tab === t.id && (
                // One shared layoutId makes the pill *slide* between tabs rather than
                // fading — the small thing that makes navigation feel physical.
                <motion.span
                  layoutId="tab-pill"
                  className="absolute inset-0 rounded-full bg-sepia"
                  transition={{ type: "spring", stiffness: 380, damping: 32 }}
                />
              )}
              <span className={tab === t.id ? "relative text-vellum" : "relative text-soft hover:text-ink"}>
                {t.label}
              </span>
            </button>
          ))}
        </nav>
      </motion.header>

      {/*
        Each tab enters as its own element rather than the container re-rendering, so the
        content crossfades and lifts instead of snapping. `mode="wait"` because two
        panels sliding over each other reads as a glitch at this size.
      */}
      <AnimatePresence mode="wait">
        <motion.main
          key={tab}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -6 }}
          transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
        >
          {children}
        </motion.main>
      </AnimatePresence>
    </div>
  );
}

/**
 * The write posture, always visible. A tool that can edit your notes should never leave
 * you guessing whether it currently can.
 */
function ModePill({ apply, autonomy }: { apply: boolean; autonomy: string }) {
  const writing = apply && autonomy !== "none";
  return (
    <span
      className={[
        "rounded-full border px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em]",
        writing
          ? "border-verdigris/40 text-verdigris"
          : "border-rule text-faint",
      ].join(" ")}
      title={
        writing
          ? `Writes are on, autonomy=${autonomy}. Low-risk changes apply on their own.`
          : "Propose-only. Nothing reaches Notion without your approval."
      }
    >
      {writing ? `writing · ${autonomy}` : "propose-only"}
    </span>
  );
}
