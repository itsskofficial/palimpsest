"use client";

import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type ActivityEntry } from "@/lib/api";

/**
 * The whole history, and the one button that makes autonomy safe to hand out.
 *
 * Every operation this system performs carries an inverse computed *before* it ran, so
 * undo is a real reversal rather than a compensating guess. That is what lets the
 * autonomy ladder go as far as it does: the answer to "it changed something I did not
 * want" is one tap here, not a manual repair in Notion with the original wording lost.
 *
 * One list rather than three. A change that applied on its own, one you approved, and one
 * you rejected are the same event from the reader's side — something was proposed and
 * this is what became of it — and splitting them by which door they came through makes
 * the history unreadable exactly when you are hunting for the change that broke
 * something.
 */
export function ActivityLog() {
  const [rows, setRows] = useState<ActivityEntry[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows((await api.activity()).activity);
    } catch {
      /* the shell surfaces disconnection */
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [load]);

  const undo = async (entry: ActivityEntry) => {
    setBusy(entry.patch_id);
    setError(null);
    try {
      await api.undo(entry.patch_id);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not undo that");
    } finally {
      setBusy(null);
    }
  };

  if (rows === null) return <p className="text-sm text-faint">Reading the ledger…</p>;
  if (!rows.length)
    return (
      <div className="rounded-xl border border-dashed border-rule px-6 py-14 text-center">
        <p className="font-display text-lg text-ink">Nothing has happened yet</p>
        <p className="mt-1 text-[13px] text-soft">
          Every change palimpsest makes will be listed here, with a way to take it back.
        </p>
      </div>
    );

  return (
    <section className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h2 className="font-display text-lg text-ink">Everything that happened</h2>
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-faint">
          {rows.length} entr{rows.length === 1 ? "y" : "ies"}
        </span>
      </div>

      {error && <p className="text-[13px] text-rubric">{error}</p>}

      <ol className="space-y-2">
        <AnimatePresence initial={false}>
          {rows.map((entry) => (
            <Row
              key={entry.patch_id}
              entry={entry}
              busy={busy === entry.patch_id}
              onUndo={() => void undo(entry)}
            />
          ))}
        </AnimatePresence>
      </ol>
    </section>
  );
}

const STATUS: Record<string, { label: string; tone: string }> = {
  applied: { label: "applied", tone: "text-verdigris border-verdigris/40" },
  reverted: { label: "undone", tone: "text-faint border-rule" },
  rejected: { label: "rejected", tone: "text-faint border-rule" },
  proposed: { label: "waiting", tone: "text-rubric border-rubric/40" },
  partial: { label: "partly applied", tone: "text-gilt border-gilt/40" },
};

function Row({
  entry,
  busy,
  onUndo,
}: {
  entry: ActivityEntry;
  busy: boolean;
  onUndo: () => void;
}) {
  const state = entry.reverted
    ? STATUS.reverted
    : (STATUS[entry.status] ?? { label: entry.status, tone: "text-faint border-rule" });

  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      className="rounded-xl border border-rule bg-raised px-4 py-3"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="truncate text-[14px] text-ink">
            {entry.source?.title || "untitled source"}
          </p>
          <p className="mt-0.5 text-[12.5px] text-soft">
            {entry.applied || entry.operations} change
            {(entry.applied || entry.operations) === 1 ? "" : "s"}
            {entry.relations.length > 0 && ` · ${entry.relations.join(", ")}`}
            {entry.pages.length > 0 && ` · ${entry.pages.slice(0, 3).join(", ")}`}
            {entry.reviewer && ` · by ${entry.reviewer}`}
          </p>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <span
            className={`rounded-full border px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${state.tone}`}
          >
            {state.label}
          </span>
          {entry.undoable && (
            <button
              onClick={onUndo}
              disabled={busy}
              className="rounded-lg border border-rule px-3 py-1 text-[13px] text-soft transition hover:border-rubric hover:text-rubric disabled:opacity-40"
            >
              {busy ? "Undoing…" : "Undo"}
            </button>
          )}
        </div>
      </div>

      <p className="mt-2 font-mono text-[10px] text-faint">
        {entry.patch_id}
        {entry.at ? ` · ${new Date(entry.at * 1000).toLocaleString()}` : ""}
      </p>
    </motion.li>
  );
}
