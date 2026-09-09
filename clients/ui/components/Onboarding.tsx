"use client";

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { api, ApiError, type LocalRuntime } from "@/lib/api";

/**
 * First run, in the app.
 *
 * Every step validates against the real service before it lets you past, because the
 * failure this prevents is the worst kind: a setup that *looks* finished and then
 * silently does nothing — a Notion token with no page shared, a bot token typo, a chat
 * id off by a digit. Each is caught here, with the fix named.
 *
 * The Notion step is the one people get wrong, so it does the hard part for you: it
 * lists the pages your integration can actually see, and offers to create a fresh
 * top-level page if none of them are right. Nobody has to understand what "share a page
 * with an integration" means before they can start.
 */
type StepId = "welcome" | "claude" | "notion" | "root" | "telegram" | "done";

const ORDER: StepId[] = ["welcome", "claude", "notion", "root", "telegram", "done"];

export function Onboarding({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState<StepId>("welcome");
  const [notionToken, setNotionToken] = useState("");
  const idx = ORDER.indexOf(step);

  const go = (s: StepId) => setStep(s);

  return (
    <div className="mx-auto flex min-h-screen max-w-xl flex-col justify-center px-6 py-16">
      <Progress index={idx} total={ORDER.length - 1} />

      <AnimatePresence mode="wait">
        <motion.div
          key={step}
          initial={{ opacity: 0, y: 14, filter: "blur(4px)" }}
          animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
          exit={{ opacity: 0, y: -14, filter: "blur(4px)" }}
          transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
          className="mt-10"
        >
          {step === "welcome" && <Welcome onNext={() => go("claude")} />}
          {step === "claude" && <ClaudeStep onNext={() => go("notion")} />}
          {step === "notion" && (
            <NotionStep
              onNext={(tok) => {
                setNotionToken(tok);
                go("root");
              }}
            />
          )}
          {step === "root" && (
            <RootStep token={notionToken} onNext={() => go("telegram")} />
          )}
          {step === "telegram" && (
            <TelegramStep onNext={() => go("done")} onSkip={() => go("done")} />
          )}
          {step === "done" && <Done onFinish={onDone} />}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

function Progress({ index, total }: { index: number; total: number }) {
  return (
    <div className="flex items-center gap-2">
      {Array.from({ length: total }).map((_, i) => (
        <motion.div
          key={i}
          className="h-1 flex-1 rounded-full bg-rule"
          animate={{
            backgroundColor:
              i < index ? "rgb(var(--sepia))" : "rgb(var(--rule))",
          }}
          transition={{ duration: 0.4 }}
        />
      ))}
    </div>
  );
}

function Title({ children, sub }: { children: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <>
      <h1 className="text-balance font-display text-3xl leading-tight text-ink">
        {children}
      </h1>
      {sub && <p className="mt-3 text-[15px] leading-relaxed text-soft">{sub}</p>}
    </>
  );
}

function Primary({
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      className="rounded-lg bg-sepia px-5 py-2.5 text-sm font-medium text-vellum transition hover:opacity-90 disabled:opacity-40"
    >
      {children}
    </button>
  );
}

function Field({
  value,
  onChange,
  placeholder,
  onEnter,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  onEnter?: () => void;
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => e.key === "Enter" && onEnter?.()}
      placeholder={placeholder}
      autoFocus
      className="w-full rounded-lg border border-rule bg-raised px-3.5 py-2.5 font-mono text-[13px] text-ink placeholder:text-faint focus:border-sepia focus:outline-none"
    />
  );
}

function Note({ kind, children }: { kind: "ok" | "err"; children: React.ReactNode }) {
  return (
    <motion.p
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      className={`text-[13px] ${kind === "ok" ? "text-verdigris" : "text-rubric"}`}
    >
      {children}
    </motion.p>
  );
}

// -- steps -------------------------------------------------------------------

function Welcome({ onNext }: { onNext: () => void }) {
  return (
    <div>
      <Title
        sub={
          <>
            Give it anything — a link, a PDF, a voice note, a thought — and it works out
            how that relates to what you have already written, then proposes small,
            reversible, cited edits to the right pages.
            <br />
            <br />
            Three things to set up. Two minutes.
          </>
        }
      >
        Your notes, rewritten as you learn.
      </Title>
      <div className="mt-8">
        <Primary onClick={onNext}>Get started</Primary>
      </div>
    </div>
  );
}

/**
 * The providers the wizard offers, and what each one costs you to start.
 *
 * palimpsest speaks the OpenAI chat-completions standard, so this list is a convenience
 * rather than a limit — anything with a compatible endpoint works by setting a base URL
 * in Settings afterwards. What the wizard owes a first-time user is a short list with an
 * honest note on each, not every option there is.
 */
const PROVIDERS: {
  id: string;
  label: string;
  where: string;
  hint: string;
  placeholder: string;
  keyName: string;
}[] = [
  {
    id: "anthropic",
    label: "Claude",
    where: "console.anthropic.com → API keys",
    hint: "What the classifier was built and measured against.",
    placeholder: "sk-ant-…",
    keyName: "ANTHROPIC_API_KEY",
  },
  {
    id: "openai",
    label: "OpenAI",
    where: "platform.openai.com → API keys",
    hint: "Also unlocks embeddings, which help it find a page you worded differently.",
    placeholder: "sk-…",
    keyName: "OPENAI_API_KEY",
  },
  {
    id: "groq",
    label: "Groq",
    where: "console.groq.com → API keys",
    hint: "Fast and very cheap. Open models score lower on the classifier eval — run `palimpsest eval component` before trusting it with autonomy.",
    placeholder: "gsk_…",
    keyName: "GROQ_API_KEY",
  },
  {
    id: "ollama",
    label: "On this machine",
    where: "ollama.com, then: ollama pull qwen2.5:7b",
    hint: "No key, no cost, nothing leaves your computer. It also gives you free embeddings, which is what finds a page you worded completely differently. Small models classify noticeably worse, so measure yours with palimpsest eval component before giving it autonomy.",
    placeholder: "",
    keyName: "",
  },
];

function ClaudeStep({ onNext }: { onNext: () => void }) {
  const [provider, setProvider] = useState(PROVIDERS[0]);
  const [token, setToken] = useState("");
  const [state, setState] = useState<"idle" | "busy" | "ok" | "err">("idle");
  const [msg, setMsg] = useState("");
  const [local, setLocal] = useState<LocalRuntime | null>(null);
  const [localModel, setLocalModel] = useState("");

  // Asked once, up front, so the local option can say *which* models are already here
  // rather than telling you to go and find out. A machine running nothing simply never
  // shows the list, and the option explains what to install.
  useEffect(() => {
    api
      .localRuntimes()
      .then((r) => {
        const first = r.runtimes[0];
        if (!first) return;
        setLocal(first);
        setLocalModel(first.suggested_model);
      })
      .catch(() => {});
  }, []);

  const isLocal = provider.id === "ollama";

  const submit = async () => {
    if (isLocal) {
      if (!localModel) return;
      setState("busy");
      try {
        await api.saveSettings({
          PALIMPSEST_MODEL_PROVIDER: "ollama",
          PALIMPSEST_MODEL: localModel,
          // Vectors from the same runtime. This is the only configuration where they
          // cost nothing, so it would be perverse to make it a second decision.
          ...(local?.suggested_embedding
            ? {
                PALIMPSEST_EMBED_PROVIDER: "ollama",
                PALIMPSEST_EMBED_MODEL: local.suggested_embedding,
              }
            : {}),
        });
        setState("ok");
        setTimeout(onNext, 500);
      } catch (e) {
        setState("err");
        setMsg(e instanceof ApiError ? e.message : "Could not save");
      }
      return;
    }
    if (!token.trim()) return;
    setState("busy");
    try {
      // Only Anthropic keys have a cheap shape check; the rest are validated by their
      // first real call, which fails with a clear message rather than a mystery.
      if (provider.id === "anthropic") {
        const r = await api.validate("anthropic", token.trim());
        if (!r.ok) {
          setState("err");
          setMsg(r.detail ?? r.error ?? "That does not look right");
          return;
        }
      }
      await api.saveSettings({
        [provider.keyName]: token.trim(),
        PALIMPSEST_MODEL_PROVIDER: provider.id,
      });
      setState("ok");
      setTimeout(onNext, 500);
    } catch (e) {
      setState("err");
      setMsg(e instanceof ApiError ? e.message : "Could not save");
    }
  };

  return (
    <div>
      <Title sub="It reads what you send, pulls out the claims, and works out how each one relates to your notes.">
        The brain
      </Title>

      <div className="mt-4 flex gap-2">
        {PROVIDERS.map((p) => (
          <button
            key={p.id}
            onClick={() => {
              setProvider(p);
              setState("idle");
            }}
            className={`rounded-lg border px-3 py-1.5 text-[13px] transition ${
              provider.id === p.id
                ? "border-sepia bg-sepia/10 text-ink"
                : "border-rule text-soft hover:border-sepia/50"
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>

      <p className="mt-3 text-[13px] text-soft">{provider.hint}</p>
      <p className="mt-1 text-[13px] text-faint">Get a key at {provider.where}</p>

      <div className="mt-3 space-y-3">
        {isLocal ? (
          local ? (
            <div className="space-y-2">
              <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-verdigris">
                {local.provider} is running here
              </p>
              <div className="flex flex-wrap gap-2">
                {local.models.map((m) => (
                  <button
                    key={m}
                    onClick={() => setLocalModel(m)}
                    className={`rounded-lg border px-3 py-1.5 font-mono text-[12px] transition ${
                      localModel === m
                        ? "border-sepia bg-sepia/10 text-ink"
                        : "border-rule text-soft hover:border-sepia/50"
                    }`}
                  >
                    {m}
                  </button>
                ))}
              </div>
              {local.suggested_embedding && (
                <p className="text-[12.5px] text-soft">
                  Vectors from{" "}
                  <span className="font-mono text-sepia">
                    {local.suggested_embedding}
                  </span>{" "}
                  as well.
                </p>
              )}
            </div>
          ) : (
            <Note kind="err">
              Nothing is serving models on this machine yet. Install Ollama, then run
              ollama pull qwen2.5:7b and ollama pull mxbai-embed-large.
            </Note>
          )
        ) : (
          <Field
            value={token}
            onChange={setToken}
            placeholder={provider.placeholder}
            onEnter={submit}
          />
        )}
        {state === "err" && <Note kind="err">{msg}</Note>}
        {state === "ok" && <Note kind="ok">Saved</Note>}
        <Primary
          onClick={submit}
          disabled={state === "busy" || (isLocal ? !localModel : !token.trim())}
        >
          {state === "busy" ? "Checking…" : "Continue"}
        </Primary>
        <p className="text-[12px] text-faint">
          Anything else that speaks the OpenAI API — OpenRouter, a local Ollama — works
          too. Add it from Settings once you are set up.
        </p>
      </div>
    </div>
  );
}


function NotionStep({ onNext }: { onNext: (token: string) => void }) {
  const [token, setToken] = useState("");
  const [state, setState] = useState<"idle" | "busy" | "err">("idle");
  const [msg, setMsg] = useState("");

  const submit = async () => {
    if (!token.trim()) return;
    setState("busy");
    try {
      const r = await api.validate("notion", token.trim());
      if (!r.ok) {
        setState("err");
        setMsg(r.error ?? "Notion rejected that token");
        return;
      }
      await api.saveSettings({ NOTION_TOKEN: token.trim() });
      onNext(token.trim());
    } catch (e) {
      setState("err");
      setMsg(e instanceof ApiError ? e.message : "Could not save");
    }
  };

  return (
    <div>
      <Title sub="Where your notes live. palimpsest reads them, and proposes edits you approve.">
        Notion
      </Title>
      <ol className="mt-4 space-y-1.5 text-[13px] text-soft">
        <li>1. Go to notion.so/my-integrations → New integration</li>
        <li>2. Give it Read, Update and Insert content</li>
        <li>3. Copy the token (it starts with ntn_)</li>
        <li className="text-ink">
          4. Open a Notion page → ⋯ → Connections → your integration.{" "}
          <span className="text-faint">
            An integration sees nothing until a page is shared with it — this is the step
            everyone misses.
          </span>
        </li>
      </ol>
      <div className="mt-4 space-y-3">
        <Field value={token} onChange={setToken} placeholder="ntn_…" onEnter={submit} />
        {state === "err" && <Note kind="err">{msg}</Note>}
        <Primary onClick={submit} disabled={state === "busy" || !token.trim()}>
          {state === "busy" ? "Connecting…" : "Continue"}
        </Primary>
      </div>
    </div>
  );
}

function RootStep({ token, onNext }: { token: string; onNext: () => void }) {
  const [pages, setPages] = useState<{ page_id: string; title: string }[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .notionPages(token)
      .then((r) => setPages(r.pages))
      .catch(() => setPages([]));
  }, [token]);

  const choose = async (pageId: string) => {
    setBusy(true);
    try {
      await api.saveSettings({ PALIMPSEST_NOTION_ROOTS: pageId });
      onNext();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not save");
      setBusy(false);
    }
  };

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.createRoot(token, "palimpsest");
      await api.saveSettings({ PALIMPSEST_NOTION_ROOTS: r.page_id });
      onNext();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not create a page");
      setBusy(false);
    }
  };

  return (
    <div>
      <Title sub="palimpsest works inside one page and everything under it. The rest of your workspace stays untouched — that boundary is enforced in code, not by good intentions.">
        Where should it live?
      </Title>

      <div className="mt-5 space-y-2">
        {pages === null && <p className="text-[13px] text-faint">Looking…</p>}

        {pages?.length ? (
          <>
            <p className="font-mono text-[10px] uppercase tracking-wider text-faint">
              pages your integration can see
            </p>
            <div className="max-h-56 space-y-1.5 overflow-y-auto pr-1">
              {pages.map((p) => (
                <motion.button
                  key={p.page_id}
                  whileHover={{ x: 3 }}
                  disabled={busy}
                  onClick={() => void choose(p.page_id)}
                  className="flex w-full items-center justify-between rounded-lg border border-rule px-3.5 py-2.5 text-left transition hover:border-sepia disabled:opacity-50"
                >
                  <span className="truncate text-[14px] text-ink">{p.title}</span>
                  <span className="ml-3 shrink-0 font-mono text-[10px] text-faint">
                    {p.page_id.slice(0, 8)}
                  </span>
                </motion.button>
              ))}
            </div>
          </>
        ) : null}

        <button
          onClick={() => void create()}
          disabled={busy}
          className="w-full rounded-lg border border-dashed border-rule px-3.5 py-2.5 text-left text-[14px] text-soft transition hover:border-sepia hover:text-ink disabled:opacity-50"
        >
          {busy ? "Working…" : "Create a fresh “palimpsest” page for me"}
          <span className="mt-0.5 block text-[12px] text-faint">
            Made at the top level, so it sits inside none of your existing notes.
          </span>
        </button>

        {error && <Note kind="err">{error}</Note>}
      </div>
    </div>
  );
}

function TelegramStep({ onNext, onSkip }: { onNext: () => void; onSkip: () => void }) {
  const [token, setToken] = useState("");
  const [bot, setBot] = useState<string | null>(null);
  const [phase, setPhase] = useState<"token" | "pair">("token");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const check = async () => {
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.validate("telegram", token.trim());
      if (!r.ok) {
        setError(r.error ?? "Telegram rejected that token");
        return;
      }
      await api.saveSettings({ TELEGRAM_BOT_TOKEN: token.trim() });
      setBot(r.username ?? "your bot");
      setPhase("pair");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not check that");
    } finally {
      setBusy(false);
    }
  };

  const pair = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.pairTelegram(token.trim());
      if (r.paired && r.chat_id) {
        await api.saveSettings({ TELEGRAM_ALLOWED_CHATS: String(r.chat_id) });
        onNext();
      } else {
        setError("No message arrived. Send your bot a message, then try again.");
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Pairing failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <Title sub="Optional. Adds a bot you can send things to — and approve changes from — anywhere, without opening this app.">
        Telegram
      </Title>

      {phase === "token" ? (
        <div className="mt-5 space-y-3">
          <p className="text-[13px] text-soft">
            Message <span className="font-mono text-sepia">@BotFather</span> on Telegram,
            send <span className="font-mono text-sepia">/newbot</span>, and paste the
            token here.
          </p>
          <Field
            value={token}
            onChange={setToken}
            placeholder="123456:ABC-DEF…"
            onEnter={check}
          />
          {error && <Note kind="err">{error}</Note>}
          <div className="flex items-center gap-3">
            <Primary onClick={check} disabled={busy || !token.trim()}>
              {busy ? "Checking…" : "Continue"}
            </Primary>
            <button
              onClick={onSkip}
              className="text-[13px] text-faint underline-offset-4 hover:text-soft hover:underline"
            >
              Skip — I will use the app
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-5 space-y-3">
          <Note kind="ok">Found @{bot}</Note>
          <p className="text-[13px] text-soft">
            Now open Telegram and send <span className="text-ink">@{bot}</span> any
            message. That pairs your account — the bot refuses everyone else.
          </p>
          {error && <Note kind="err">{error}</Note>}
          <div className="flex items-center gap-3">
            <Primary onClick={pair} disabled={busy}>
              {busy ? "Waiting for your message…" : "I have messaged it"}
            </Primary>
            <button
              onClick={onSkip}
              className="text-[13px] text-faint underline-offset-4 hover:text-soft hover:underline"
            >
              Do this later
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function Done({ onFinish }: { onFinish: () => void }) {
  return (
    <div>
      <motion.div
        initial={{ scale: 0.9, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ type: "spring", stiffness: 240, damping: 20 }}
        className="mb-6 flex w-28 flex-col gap-1.5"
      >
        {[0, 1, 2].map((i) => (
          <motion.div
            key={i}
            className="h-2 rounded-full bg-sepia"
            initial={{ scaleX: 0 }}
            animate={{ scaleX: 1 }}
            transition={{ delay: 0.15 + i * 0.12, duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
            style={{ originX: 0, opacity: 1 - i * 0.28 }}
          />
        ))}
      </motion.div>

      <Title sub="Everything is set. Drop something in and watch what it does — nothing reaches Notion without your approval until you say otherwise.">
        Ready.
      </Title>
      <div className="mt-8">
        <Primary onClick={onFinish}>Open palimpsest</Primary>
      </div>
    </div>
  );
}
