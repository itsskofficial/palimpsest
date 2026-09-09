"use client";

import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";
import { api, ApiError, type Approval, type Operation } from "@/lib/api";

/** Relation → the colour it carries everywhere in the UI. */
const TONE: Record<string, string> = {
  corroborates: "text-verdigris",
  new: "text-sepia",
  refines: "text-gilt",
  supersedes: "text-rubric",
  duplicate: "text-soft",
  extends: "text-sepia",
  contradicts: "text-rubric",
};

export function ApprovalCard({
  approval,
  onResolved,
}: {
  approval: Approval;
  onResolved: () => void;
}) {
  const [open, setOpen] = useState(approval.operations.length <= 3);
  const [busy, setBusy] = useState<"approved" | "rejected" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const decide = async (decision: "approved" | "rejected") => {
    setBusy(decision);
    setError(null);
    try {
      await api.resolve(approval.approval_id, decision);
      onResolved();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't do that");
      setBusy(null);
    }
  };

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, scale: 0.97, transition: { duration: 0.18 } }}
      transition={{ type: "spring", stiffness: 300, damping: 28 }}
      className="overflow-hidden rounded-xl border border-rubric/30 bg-raised shadow-sheet"
    >
      {/* The rubric spine: red for what demands the reader's attention. */}
      <div className="flex">
        <div className="w-1 shrink-0 bg-rubric/70" />
        <div className="min-w-0 flex-1 p-4">
          <button
            onClick={() => setOpen((o) => !o)}
            className="flex w-full items-start justify-between gap-3 text-left"
          >
            <div className="min-w-0">
              <p className="truncate font-medium text-ink">
                {approval.summary || "A change to your notes"}
              </p>
              <p className="mt-0.5 text-[13px] text-soft">
                {approval.operations.length} change
                {approval.operations.length === 1 ? "" : "s"}
                {approval.source?.title && (
                  <> · from <span className="text-faint">{approval.source.title}</span></>
                )}
              </p>
            </div>
            <motion.span
              animate={{ rotate: open ? 180 : 0 }}
              transition={{ duration: 0.2 }}
              className="mt-1 shrink-0 text-faint"
            >
              ▾
            </motion.span>
          </button>

          <AnimatePresence initial={false}>
            {open && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
                className="overflow-hidden"
              >
                <div className="mt-3 space-y-3 border-t border-rule pt-3">
                  {approval.operations.map((op) => (
                    <OperationDiff key={op.op_id} op={op} />
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {error && <p className="mt-3 text-[13px] text-rubric">{error}</p>}

          <div className="mt-4 flex items-center gap-2">
            <button
              onClick={() => void decide("approved")}
              disabled={!!busy}
              className="rounded-lg bg-verdigris px-4 py-1.5 text-sm font-medium text-vellum transition hover:opacity-90 disabled:opacity-50"
            >
              {busy === "approved" ? "Applying…" : "Approve"}
            </button>
            <button
              onClick={() => void decide("rejected")}
              disabled={!!busy}
              className="rounded-lg border border-rule px-4 py-1.5 text-sm text-soft transition hover:border-rubric hover:text-rubric disabled:opacity-50"
            >
              Reject
            </button>
            <span className="ml-auto font-mono text-[10px] text-faint">
              {approval.patch_id}
            </span>
          </div>
        </div>
      </div>
    </motion.div>
  );
}

/**
 * One operation, rendered as the product's own metaphor: where text is being replaced,
 * the old wording stays visible and struck beneath the new. That is what a palimpsest
 * *is*, and it is also the honest way to show an edit — you can see what you are losing.
 */
function OperationDiff({ op }: { op: Operation }) {
  return (
    <div className="text-[14px]">
      <div className="mb-1 flex items-baseline gap-2">
        <span className={`font-mono text-[10px] uppercase tracking-wider ${TONE[op.relation ?? ""] ?? "text-faint"}`}>
          {op.relation ?? op.kind.replace(/_/g, " ")}
        </span>
        <span className="truncate text-[12px] text-faint">on {op.page}</span>
        {typeof op.confidence === "number" && (
          <span className="ml-auto shrink-0 font-mono text-[10px] text-faint">
            {Math.round(op.confidence * 100)}%
          </span>
        )}
      </div>

      {op.was && <p className="ghost leading-relaxed">{op.was}</p>}
      {op.text && (
        <motion.p
          initial={{ opacity: 0, y: 3 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: op.was ? 0.12 : 0 }}
          className="leading-relaxed text-ink"
        >
          {op.text}
        </motion.p>
      )}
      {!op.text && !op.was && (
        <p className="text-soft">{op.kind.replace(/_/g, " ")}</p>
      )}

      {op.why && (
        <p className="mt-1 border-l-2 border-rule pl-2 text-[12.5px] italic text-faint">
          {op.why}
        </p>
      )}
    </div>
  );
}
