"use client";

import { motion } from "motion/react";
import type { SetupState } from "@/lib/api";

export type Tab = "capture" | "ask" | "settings";

const TABS: { id: Tab; label: string }[] = [
  { id: "capture", label: "Capture" },
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
    <div className="mx-auto min-h-screen max-w-3xl px-6 pb-24 pt-8">
      <header className="mb-10 flex items-center justify-between">
        <div className="flex items-baseline gap-3">
          <h1 className="font-display text-2xl font-semibold tracking-tight text-ink">
            palimpsest
          </h1>
          <ModePill apply={setup.apply} autonomy={setup.autonomy} />
        </div>

        <nav className="flex items-center gap-1 rounded-full border border-rule bg-raised/60 p-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => onTab(t.id)}
              className="relative rounded-full px-3.5 py-1.5 text-sm font-medium transition-colors"
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
      </header>

      <main>{children}</main>
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
