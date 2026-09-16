"use client";

import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";
import { ApprovalCard } from "@/components/ApprovalCard";
import { api, subscribe, type Approval, type JobEvent } from "@/lib/api";

/**
 * One stream for everything that happens: sources arriving, claims coming out, and the
 * changes waiting on you — in the order it happened.
 *
 * Merging approvals into the same feed rather than exiling them to a separate "review"
 * screen is the point. The product's promise is "drop something in and watch your notes
 * change"; splitting the watching from the deciding would break that into two chores.
 */
export function ActivityFeed({
  refreshKey,
  limit,
  onSeeAll,
}: {
  refreshKey: number;
  /** How many captures to show. The home page passes 1 — see the note in `page.tsx`. */
  limit?: number;
  onSeeAll?: () => void;
}) {
  const [jobs, setJobs] = useState<Record<string, JobEvent>>({});
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [live, setLive] = useState(false);

  const loadAll = useCallback(async () => {
    try {
      const [j, a] = await Promise.all([api.jobs(30), api.approvals("pending")]);
      const map: Record<string, JobEvent> = {};
      for (const row of j.jobs) {
        const r = row.result ?? {};
        map[row.job_id] = {
          job_id: row.job_id,
          status: row.status,
          title: row.title || row.url || (row.spec ?? "").slice(0, 60),
          kind: r.source?.kind,
          claims: r.claims,
          by_relation: r.patch?.by_relation,
          applied: r.auto_applied?.applied,
          held: r.auto_applied?.held,
          approval_id: r.auto_applied?.approval_id,
          error: row.error,
        };
      }
      setJobs(map);
      setApprovals(a.approvals);
    } catch {
      /* the shell surfaces disconnection */
    }
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll, refreshKey]);

  useEffect(() => {
    return subscribe({
      onOpen: () => setLive(true),
      onError: () => setLive(false),
      onJob: (e) => setJobs((prev) => ({ ...prev, [e.job_id]: e })),
      onApproval: () => void loadAll(),
    });
  }, [loadAll]);

  const all = Object.values(jobs);
  const rows = limit ? all.slice(0, limit) : all;
  const hidden = all.length - rows.length;
  const pending = approvals.length;

  if (!rows.length && !pending) return <Empty live={live} />;

  return (
    <section>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="font-display text-lg text-ink">
          {limit ? "Latest" : "Activity"}
        </h2>
        <span className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.16em] text-faint">
          <motion.span
            animate={live ? { opacity: [0.35, 1, 0.35] } : { opacity: 0.3 }}
            transition={{ duration: 2.4, repeat: Infinity }}
            className={`h-1.5 w-1.5 rounded-full ${live ? "bg-verdigris" : "bg-faint"}`}
          />
          {live ? "live" : "reconnecting"}
        </span>
      </div>

      {pending > 0 && (
        <motion.div layout className="mb-6 space-y-3">
          <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-rubric">
            {pending} waiting for you
          </p>
          <AnimatePresence initial={false}>
            {approvals.map((a) => (
              <ApprovalCard
                key={a.approval_id}
                approval={a}
                onResolved={() => {
                  setApprovals((prev) =>
                    prev.filter((x) => x.approval_id !== a.approval_id),
                  );
                  void loadAll();
                }}
              />
            ))}
          </AnimatePresence>
        </motion.div>
      )}

      {/* The strata: each capture is a layer settling onto the ones before it. */}
      <ol className="relative space-y-2 border-l border-rule pl-5">
        <AnimatePresence initial={false}>
          {rows.map((job) => (
            <JobRow key={job.job_id} job={job} />
          ))}
        </AnimatePresence>
      </ol>

      {hidden > 0 && onSeeAll && (
        <button
          onClick={onSeeAll}
          className="mt-4 font-mono text-[11px] uppercase tracking-[0.14em] text-faint transition hover:text-sepia"
        >
          {hidden} more in Activity &rarr;
        </button>
      )}
    </section>
  );
}

function JobRow({ job }: { job: JobEvent }) {
  const running = job.status === "running" || job.status === "queued";
  const failed = job.status === "failed";

  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: -8, filter: "blur(4px)" }}
      animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ type: "spring", stiffness: 320, damping: 30 }}
      className="relative rounded-lg px-3 py-2.5 transition-colors hover:bg-raised/70"
    >
      <span
        className={[
          "absolute -left-[25px] top-4 h-2 w-2 rounded-full ring-4 ring-vellum",
          failed ? "bg-rubric" : running ? "bg-gilt" : "bg-verdigris",
        ].join(" ")}
      />
      {running && (
        <motion.span
          className="absolute -left-[27px] top-[14px] h-3 w-3 rounded-full bg-gilt/40"
          animate={{ scale: [1, 1.9], opacity: [0.5, 0] }}
          transition={{ duration: 1.6, repeat: Infinity, ease: "easeOut" }}
        />
      )}

      <div className="flex items-baseline justify-between gap-3">
        <p className="truncate text-[15px] text-ink">{job.title || "Untitled"}</p>
        {job.kind && (
          <span className="shrink-0 font-mono text-[10px] uppercase tracking-wider text-faint">
            {job.kind}
          </span>
        )}
      </div>

      <p className="mt-0.5 text-[13px] text-soft">
        {failed ? (
          <span className="text-rubric">{job.error?.slice(0, 140) || "failed"}</span>
        ) : running ? (
          <ShimmerText>reading it…</ShimmerText>
        ) : (
          <Outcome job={job} />
        )}
      </p>
    </motion.li>
  );
}

function Outcome({ job }: { job: JobEvent }) {
  if (job.playlist) {
    const p = job.playlist;
    return (
      <>
        queued {p.videos} video{p.videos === 1 ? "" : "s"}, each ingested on its own
        {!!p.skipped && <> · {p.skipped} unavailable</>}
        {p.truncated && <> · first {p.videos} of {p.total}</>}
      </>
    );
  }
  if (!job.claims) return <>nothing worth keeping came out of it</>;
  const rel = Object.entries(job.by_relation ?? {})
    .map(([k, v]) => `${v} ${k}`)
    .join(", ");
  return (
    <>
      {job.claims} claim{job.claims === 1 ? "" : "s"}
      {rel && <> → {rel}</>}
      {!!job.applied && <span className="text-verdigris"> · {job.applied} applied</span>}
      {!!job.held && <span className="text-rubric"> · {job.held} awaiting you</span>}
    </>
  );
}

/** A slow sweep across the text while work is in flight. */
function ShimmerText({ children }: { children: React.ReactNode }) {
  return (
    <motion.span
      className="bg-gradient-to-r from-soft via-gilt to-soft bg-[length:200%_100%] bg-clip-text text-transparent"
      animate={{ backgroundPosition: ["200% 0", "-200% 0"] }}
      transition={{ duration: 2.2, repeat: Infinity, ease: "linear" }}
    >
      {children}
    </motion.span>
  );
}

function Empty({ live }: { live: boolean }) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="rounded-xl border border-dashed border-rule px-6 py-10 text-center"
    >
      {/*
        A quill nib, drawn rather than imported: three paths, no icon dependency, and it
        inherits `currentColor` so it is correct in both themes without a second asset.
        It breathes, faintly, so an empty screen is not a dead one.
      */}
      <motion.svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="mx-auto mb-3 h-7 w-7 text-faint"
        animate={{ opacity: [0.45, 0.8, 0.45], y: [0, -2, 0] }}
        transition={{ duration: 4.5, repeat: Infinity, ease: "easeInOut" }}
        aria-hidden
      >
        <path d="M3 21c2.5-4.5 6-9 11-12.5" />
        <path d="M20.5 3.5c-1 5.5-4 9-7.5 10.5-2 .9-4 .6-5-.4s-1.3-3-.4-5C9.1 5.1 13 2.5 20.5 3.5Z" />
        <path d="M13.5 10.5 21 3" />
      </motion.svg>
      <p className="font-display text-lg text-ink/80">Nothing yet</p>
      <p className="mx-auto mt-2 max-w-sm text-[14px] leading-relaxed text-soft">
        Drop a PDF, paste a link, or write a thought above. Everything that happens to
        your notes shows up here — and anything that would change what you already wrote
        waits for your approval.
      </p>
      {!live && (
        <p className="mt-4 font-mono text-[10px] uppercase tracking-widest text-faint">
          connecting…
        </p>
      )}
    </motion.div>
  );
}
