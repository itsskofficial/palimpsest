"use client";

import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { DemoPrompts } from "./DemoPrompts";

/**
 * The surface you throw things at.
 *
 * Drag handling counts enter/leave rather than toggling on each event: `dragleave` fires
 * every time the cursor crosses a child element, so a naive toggle makes the whole
 * surface flicker while a file is still over it.
 *
 * Paste is wired to the window, not the textarea, because the instinct is to hit ⌘V
 * wherever you are — and a capture tool that ignores that has lost the thing you meant
 * to keep.
 */
export function DropZone({ onCaptured }: { onCaptured: () => void }) {
  const [text, setText] = useState("");
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [flash, setFlash] = useState<{ kind: "ok" | "err"; msg: string } | null>(null);
  const depth = useRef(0);

  const say = (kind: "ok" | "err", msg: string) => {
    setFlash({ kind, msg });
    setTimeout(() => setFlash(null), 4000);
  };

  const sendFiles = useCallback(
    async (files: File[]) => {
      if (!files.length) return;
      setBusy(true);
      try {
        const r = await api.upload(files);
        say("ok", `${r.count} file${r.count > 1 ? "s" : ""} captured — reading now`);
        onCaptured();
      } catch (e) {
        say("err", e instanceof ApiError ? e.message : "Upload failed");
      } finally {
        setBusy(false);
      }
    },
    [onCaptured],
  );

  const sendText = useCallback(async () => {
    const body = text.trim();
    if (!body) return;
    setBusy(true);
    try {
      // A bare URL is a source to fetch; anything else is text. Guessing wrong toward
      // "text" would turn a link into a note whose entire content is the link.
      const isUrl = /^https?:\/\/\S+$/i.test(body);
      await api.capture(isUrl ? { spec: body } : { text: body });
      setText("");
      say("ok", isUrl ? "Fetching that link" : "Captured");
      onCaptured();
    } catch (e) {
      say("err", e instanceof ApiError ? e.message : "Capture failed");
    } finally {
      setBusy(false);
    }
  }, [text, onCaptured]);

  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const files = Array.from(e.clipboardData?.files ?? []);
      if (files.length) {
        e.preventDefault();
        void sendFiles(files);
      }
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [sendFiles]);

  return (
    <section>
      <motion.div
        onDragEnter={(e) => {
          e.preventDefault();
          depth.current += 1;
          setDragging(true);
        }}
        onDragOver={(e) => e.preventDefault()}
        onDragLeave={() => {
          depth.current = Math.max(0, depth.current - 1);
          if (depth.current === 0) setDragging(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          depth.current = 0;
          setDragging(false);
          const files = Array.from(e.dataTransfer.files);
          if (files.length) return void sendFiles(files);
          const dropped =
            e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain");
          if (dropped) setText(dropped.trim());
        }}
        animate={{
          scale: dragging ? 1.012 : 1,
          borderColor: dragging ? "rgb(var(--sepia))" : "rgb(var(--rule))",
        }}
        transition={{ type: "spring", stiffness: 300, damping: 26 }}
        className="relative overflow-hidden rounded-2xl border-2 border-dashed bg-raised/70 p-6 shadow-sheet backdrop-blur-sm"
      >
        {/* The wash that sweeps across the vellum while a file is over it. */}
        <AnimatePresence>
          {dragging && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="pointer-events-none absolute inset-0 bg-gradient-to-br from-gilt/15 via-transparent to-sepia/10"
            />
          )}
        </AnimatePresence>

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void sendText();
          }}
          rows={4}
          placeholder="Drop a file, paste a link, or write a thought…"
          className="relative w-full resize-none bg-transparent font-sans text-[15px] leading-relaxed text-ink placeholder:text-faint focus:outline-none"
        />

        <div className="relative mt-3 flex items-center justify-between gap-4">
          <p className="font-mono text-[11px] text-faint">
            PDF · image · audio · spreadsheet · link · text
          </p>
          <div className="flex items-center gap-2">
            <label className="cursor-pointer rounded-lg border border-rule px-3 py-1.5 text-sm text-soft transition hover:border-sepia hover:text-ink">
              Browse
              <input
                type="file"
                multiple
                className="hidden"
                onChange={(e) => void sendFiles(Array.from(e.target.files ?? []))}
              />
            </label>
            <button
              onClick={() => void sendText()}
              disabled={!text.trim() || busy}
              className="rounded-lg bg-sepia px-4 py-1.5 text-sm font-medium text-vellum transition hover:opacity-90 disabled:opacity-40"
            >
              {busy ? "Sending…" : "Capture"}
            </button>
          </div>
        </div>

        <AnimatePresence>
          {dragging && (
            <motion.div
              initial={{ opacity: 0, scale: 0.98 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.98 }}
              className="absolute inset-0 flex items-center justify-center rounded-2xl bg-vellum/85 backdrop-blur-[2px]"
            >
              <span className="font-display text-xl text-sepia">Drop to capture</span>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>

      <AnimatePresence>
        {flash && (
          <motion.p
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className={`mt-3 text-sm ${flash.kind === "ok" ? "text-verdigris" : "text-rubric"}`}
          >
            {flash.msg}
          </motion.p>
        )}
      </AnimatePresence>

      <DemoPrompts onPick={setText} />
    </section>
  );
}
