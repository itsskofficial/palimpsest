"use client";

import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";

/**
 * Telegram, configured rather than assumed.
 *
 * The bot is optional now that there is a UI — so this block says what it buys you and
 * lets you turn it on, instead of it being a startup requirement.
 *
 * The pairing step is the reason this is a component and not two text inputs. A chat id
 * is not something anyone knows or can look up easily, so instead of asking for it we
 * ask Telegram: you message the bot, the backend long-polls for that first message, and
 * we take the id from it. Nobody types a number.
 */
export function TelegramPairing({
  tokenPresent,
  paired,
  onSave,
}: {
  tokenPresent: boolean;
  paired: boolean;
  onSave: (values: Record<string, string>) => void;
}) {
  const [open, setOpen] = useState(!tokenPresent);
  const [token, setToken] = useState("");
  const [checking, setChecking] = useState(false);
  const [bot, setBot] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const check = async () => {
    if (!token.trim()) return;
    setChecking(true);
    setError(null);
    try {
      const r = await api.validate("telegram", token.trim());
      if (r.ok) {
        setBot(r.username ?? r.detail ?? "your bot");
        onSave({ TELEGRAM_BOT_TOKEN: token.trim() });
      } else {
        setError(r.error ?? "Telegram rejected that token");
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not check that token");
    } finally {
      setChecking(false);
    }
  };

  const pair = async () => {
    setWaiting(true);
    setError(null);
    try {
      const r = await api.pairTelegram(token.trim());
      if (r.paired && r.chat_id) {
        onSave({ TELEGRAM_ALLOWED_CHATS: String(r.chat_id) });
      } else {
        setError("No message arrived. Send your bot a message, then try again.");
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Pairing failed");
    } finally {
      setWaiting(false);
    }
  };

  return (
    <div className="rounded-xl border border-rule bg-raised p-5">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-start justify-between gap-3 text-left"
      >
        <div>
          <h3 className="font-display text-lg text-ink">Telegram bot</h3>
          <p className="mt-0.5 text-[13px] text-soft">
            Optional. Capture and approve from your phone, anywhere.
          </p>
        </div>
        <span
          className={`shrink-0 rounded-full border px-2.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
            paired
              ? "border-verdigris/40 text-verdigris"
              : tokenPresent
                ? "border-gilt/40 text-gilt"
                : "border-rule text-faint"
          }`}
        >
          {paired ? "paired" : tokenPresent ? "needs pairing" : "off"}
        </span>
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
            <div className="mt-4 space-y-4 border-t border-rule pt-4">
              <Step n={1} title="Make a bot">
                Open Telegram, message{" "}
                <span className="font-mono text-sepia">@BotFather</span>, send{" "}
                <span className="font-mono text-sepia">/newbot</span>, and paste the token
                it gives you.
              </Step>

              <div className="flex gap-2">
                <input
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder={tokenPresent ? "a token is saved" : "123456:ABC-DEF…"}
                  className="min-w-0 flex-1 rounded-lg border border-rule bg-vellum px-3 py-2 font-mono text-[13px] text-ink placeholder:text-faint focus:border-sepia focus:outline-none"
                />
                <button
                  onClick={() => void check()}
                  disabled={checking || !token.trim()}
                  className="rounded-lg border border-rule px-4 py-2 text-sm text-soft transition hover:border-sepia hover:text-ink disabled:opacity-40"
                >
                  {checking ? "Checking…" : "Check"}
                </button>
              </div>

              <AnimatePresence>
                {bot && (
                  <motion.p
                    initial={{ opacity: 0, y: -4 }}
                    animate={{ opacity: 1, y: 0 }}
                    className="text-[13px] text-verdigris"
                  >
                    Found @{bot}
                  </motion.p>
                )}
              </AnimatePresence>

              <Step n={2} title="Pair your account">
                Message your bot anything, then press Waiting. The bot only ever answers
                the accounts you pair — an unpaired chat gets refused.
              </Step>

              <button
                onClick={() => void pair()}
                disabled={waiting || (!token.trim() && !tokenPresent)}
                className="rounded-lg bg-sepia px-4 py-2 text-sm font-medium text-vellum transition hover:opacity-90 disabled:opacity-40"
              >
                {waiting ? "Waiting for your message…" : paired ? "Re-pair" : "Pair now"}
              </button>

              {waiting && (
                <div className="flex items-center gap-1.5">
                  {[0, 1, 2].map((i) => (
                    <motion.span
                      key={i}
                      className="h-1.5 w-1.5 rounded-full bg-gilt"
                      animate={{ opacity: [0.25, 1, 0.25] }}
                      transition={{ duration: 1.2, repeat: Infinity, delay: i * 0.15 }}
                    />
                  ))}
                  <span className="ml-2 font-mono text-[11px] text-faint">
                    send your bot a message now
                  </span>
                </div>
              )}

              {error && <p className="text-[13px] text-rubric">{error}</p>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Step({
  n,
  title,
  children,
}: {
  n: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex gap-3">
      <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-sepia/12 font-mono text-[11px] text-sepia">
        {n}
      </span>
      <div>
        <p className="text-[14px] font-medium text-ink">{title}</p>
        <p className="text-[13px] leading-relaxed text-soft">{children}</p>
      </div>
    </div>
  );
}
