"use client";

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";

type Msg = { role: "you" | "pal"; text: string; tools?: string[] };

const PROMPTS = [
  "What do my notes say about…",
  "Did I write anything twice?",
  "What's waiting for my approval?",
];

/** Conversation with the agent — the same loop the Telegram bot drives. */
export function AskPanel() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  const send = async (text?: string) => {
    const body = (text ?? input).trim();
    if (!body || busy) return;
    setMessages((m) => [...m, { role: "you", text: body }]);
    setInput("");
    setBusy(true);
    try {
      const r = await api.agent(body);
      setMessages((m) => [...m, { role: "pal", text: r.text, tools: r.tools }]);
    } catch (e) {
      setMessages((m) => [
        ...m,
        { role: "pal", text: e instanceof ApiError ? e.message : "Something went wrong." },
      ]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="flex min-h-[60vh] flex-col">
      <div className="flex-1 space-y-5">
        {!messages.length && (
          <div className="rounded-xl border border-dashed border-rule px-6 py-10 text-center">
            <p className="font-display text-lg text-soft">Ask your notes anything</p>
            <p className="mt-1 text-[13px] text-faint">
              Answers come from what you&rsquo;ve actually written, with page links.
            </p>
            <div className="mt-5 flex flex-wrap justify-center gap-2">
              {PROMPTS.map((p) => (
                <button
                  key={p}
                  onClick={() => setInput(p)}
                  className="rounded-full border border-rule px-3 py-1.5 text-[13px] text-soft transition hover:border-sepia hover:text-ink"
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}

        <AnimatePresence initial={false}>
          {messages.map((m, i) => (
            <motion.div
              key={i}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ type: "spring", stiffness: 340, damping: 30 }}
              className={m.role === "you" ? "flex justify-end" : ""}
            >
              {m.role === "you" ? (
                <p className="max-w-[80%] rounded-2xl rounded-br-md bg-sepia px-4 py-2.5 text-[15px] text-vellum">
                  {m.text}
                </p>
              ) : (
                <div className="max-w-[92%]">
                  {!!m.tools?.length && (
                    <p className="mb-1.5 font-mono text-[10px] uppercase tracking-wider text-faint">
                      {Array.from(new Set(m.tools)).join(" · ")}
                    </p>
                  )}
                  <p className="whitespace-pre-wrap text-[15px] leading-relaxed text-ink">
                    {m.text}
                  </p>
                </div>
              )}
            </motion.div>
          ))}
        </AnimatePresence>

        {busy && <Thinking />}
        <div ref={endRef} />
      </div>

      <div className="sticky bottom-6 mt-6">
        <div className="flex items-end gap-2 rounded-xl border border-rule bg-raised p-2 shadow-sheet">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            rows={1}
            placeholder="Ask about your notes…"
            className="max-h-32 flex-1 resize-none bg-transparent px-2 py-1.5 text-[15px] text-ink placeholder:text-faint focus:outline-none"
          />
          <button
            onClick={() => void send()}
            disabled={!input.trim() || busy}
            className="rounded-lg bg-sepia px-4 py-2 text-sm font-medium text-vellum transition hover:opacity-90 disabled:opacity-40"
          >
            Ask
          </button>
        </div>
      </div>
    </section>
  );
}

function Thinking() {
  return (
    <div className="flex items-center gap-1.5">
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          className="h-1.5 w-1.5 rounded-full bg-gilt"
          animate={{ opacity: [0.25, 1, 0.25], y: [0, -3, 0] }}
          transition={{ duration: 1.2, repeat: Infinity, delay: i * 0.15 }}
        />
      ))}
      <span className="ml-2 font-mono text-[11px] text-faint">reading your notes</span>
    </div>
  );
}
