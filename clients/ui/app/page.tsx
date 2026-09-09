"use client";

import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";
import { ActivityFeed } from "@/components/ActivityFeed";
import { ActivityLog } from "@/components/ActivityLog";
import { AskPanel } from "@/components/AskPanel";
import { DropZone } from "@/components/DropZone";
import { Onboarding } from "@/components/Onboarding";
import { SettingsPanel } from "@/components/SettingsPanel";
import { Shell, type Tab } from "@/components/Shell";
import { api, type SetupState } from "@/lib/api";

const TABS: Tab[] = ["capture", "activity", "ask", "settings"];

/** The tab named by `#settings` and friends, so a link can land on one. */
function tabFromHash(): Tab {
  if (typeof window === "undefined") return "capture";
  const want = window.location.hash.replace("#", "");
  return (TABS as string[]).includes(want) ? (want as Tab) : "capture";
}

export default function Home() {
  const [setup, setSetup] = useState<SetupState | null>(null);
  const [offline, setOffline] = useState(false);
  const [tab, setTab] = useState<Tab>("capture");
  const [nudge, setNudge] = useState(0); // bumped to make the feed refetch

  const load = useCallback(async () => {
    try {
      setSetup(await api.setupState());
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, []);

  useEffect(() => {
    load();
    // Poll fast while we cannot see the server and slowly once we can. The desktop app
    // opens this window while the Python service is still starting, so the first minute
    // of the app's life is exactly when a quick retry matters.
    const t = setInterval(load, offline ? 2000 : 15000);
    return () => clearInterval(t);
  }, [load, offline]);

  // Hash routing, so the tray's "Settings…" can open straight onto the right tab and a
  // reload does not silently throw you back to Capture.
  useEffect(() => {
    setTab(tabFromHash());
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const goTab = useCallback((next: Tab) => {
    setTab(next);
    window.history.replaceState(null, "", next === "capture" ? " " : `#${next}`);
  }, []);

  if (offline) return <Disconnected onRetry={load} />;
  if (!setup) return <Booting />;
  if (!setup.configured) return <Onboarding onDone={load} />;

  return (
    <Shell tab={tab} onTab={goTab} setup={setup}>
      <AnimatePresence mode="wait">
        {tab === "capture" && (
          <motion.div
            key="capture"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
            className="space-y-10"
          >
            <DropZone onCaptured={() => setNudge((n) => n + 1)} />
            {/*
              Only the most recent capture. The home page answers "did that land?" —
              a question about one thing that just happened — and a full history here
              buries the drop zone under a scroll of everything you have ever sent.
              The rest lives in Activity, one tab away.
            */}
            <ActivityFeed
              refreshKey={nudge}
              limit={1}
              onSeeAll={() => goTab("activity")}
            />
          </motion.div>
        )}
        {tab === "activity" && (
          <motion.div key="activity" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={{ duration: 0.24 }}>
            <ActivityLog />
          </motion.div>
        )}
        {tab === "ask" && (
          <motion.div key="ask" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={{ duration: 0.24 }}>
            <AskPanel />
          </motion.div>
        )}
        {tab === "settings" && (
          <motion.div key="settings" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={{ duration: 0.24 }}>
            <SettingsPanel setup={setup} onSaved={load} />
          </motion.div>
        )}
      </AnimatePresence>
    </Shell>
  );
}

function Booting() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="text-center"
      >
        <Strata />
        <p className="mt-6 font-mono text-xs uppercase tracking-[0.2em] text-faint">
          waking up
        </p>
      </motion.div>
    </div>
  );
}

function Disconnected({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <div className="max-w-md text-center">
        <h1 className="font-display text-3xl text-ink">Can&rsquo;t reach the backend</h1>
        <p className="mt-3 text-soft">
          palimpsest&rsquo;s server isn&rsquo;t responding yet. If it is still starting this
          will clear on its own. If you opened this page yourself, run{" "}
          <code className="rounded bg-sunk px-1.5 py-0.5 font-mono text-sm">palimpsest serve</code>{" "}
          first.
        </p>
        <button
          onClick={onRetry}
          className="mt-6 rounded-lg bg-sepia px-5 py-2.5 font-medium text-vellum transition hover:opacity-90"
        >
          Try again
        </button>
      </div>
    </div>
  );
}

/** Three strata settling — the loading state as the product's own metaphor. */
function Strata() {
  return (
    <div className="mx-auto flex w-24 flex-col gap-1.5">
      {[0, 1, 2].map((i) => (
        <motion.div
          key={i}
          className="h-2 rounded-full bg-sepia"
          initial={{ scaleX: 0.2, opacity: 0.3 }}
          animate={{ scaleX: [0.2, 1, 0.2], opacity: [0.3, 1, 0.3] }}
          transition={{ duration: 1.6, repeat: Infinity, delay: i * 0.18, ease: "easeInOut" }}
          style={{ originX: 0 }}
        />
      ))}
    </div>
  );
}
