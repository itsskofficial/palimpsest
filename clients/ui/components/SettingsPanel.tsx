"use client";

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useState } from "react";
import { TelegramPairing } from "@/components/TelegramPairing";
import { api, ApiError, type SetupState } from "@/lib/api";

const GROUPS: {
  title: string;
  blurb: string;
  keys: { k: string; label: string; hint?: string }[];
}[] = [
  {
    title: "Essentials",
    blurb: "Without these nothing works.",
    keys: [
      { k: "ANTHROPIC_API_KEY", label: "Claude API key", hint: "console.anthropic.com" },
      { k: "NOTION_TOKEN", label: "Notion token", hint: "notion.so/my-integrations" },
      { k: "PALIMPSEST_NOTION_ROOTS", label: "Root page id", hint: "the page it works inside" },
    ],
  },
  {
    // Notion is the default and the reason most people are here, so the alternative
    // lives below it rather than in the wizard. But it has to live somewhere: until
    // these two were writable, pointing the app at a folder of notes meant editing a
    // config file by hand, which is not a thing a desktop app should ask for.
    title: "Or a folder of markdown files",
    blurb:
      "Leave these empty to use Notion. Set both to work on local notes instead — an " +
      "Obsidian vault, a git repo, anything with .md files in it.",
    keys: [
      { k: "PALIMPSEST_BACKEND", label: "Backend", hint: "notion (default) or markdown" },
      { k: "PALIMPSEST_VAULT", label: "Vault folder", hint: "the folder your notes are in" },
    ],
  },
  {
    title: "Voice notes and audio",
    blurb: "Any one of these lets you send recordings. Optional.",
    keys: [
      { k: "GROQ_API_KEY", label: "Groq", hint: "Whisper, fast, 25 MB cap" },
      { k: "DEEPGRAM_API_KEY", label: "Deepgram", hint: "long files, speaker labels" },
      { k: "SARVAM_API_KEY", label: "Sarvam", hint: "Indian languages, Hinglish" },
    ],
  },
  {
    title: "Extras",
    blurb: "Better web extraction, and tracing for costs and latency.",
    keys: [
      { k: "FIRECRAWL_API_KEY", label: "Firecrawl", hint: "JavaScript-rendered pages" },
      { k: "LANGFUSE_PUBLIC_KEY", label: "Langfuse public key" },
      { k: "LANGFUSE_SECRET_KEY", label: "Langfuse secret key" },
    ],
  },
];

//: Not secrets. Rendered as ordinary text, because a masked folder path is a field
//: nobody can check, and its saved value never appears in the placeholder either.
const PLAIN = new Set(["PALIMPSEST_BACKEND", "PALIMPSEST_VAULT"]);

export function SettingsPanel({
  setup,
  onSaved,
}: {
  setup: SetupState;
  onSaved: () => void;
}) {
  const [present, setPresent] = useState<Record<string, boolean>>({});
  const [values, setValues] = useState<Record<string, string>>({});
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const [configPath, setConfigPath] = useState("");

  const refresh = () =>
    api
      .getSettings()
      .then((s) => {
        setPresent(s.present);
        setValues(s.values);
        setConfigPath(s.config_path);
      })
      .catch(() => {});

  useEffect(() => {
    void refresh();
  }, []);

  const save = async (extra: Record<string, string> = {}) => {
    const payload = { ...edits, ...extra };
    if (!Object.keys(payload).length) return;
    setSaving(true);
    try {
      await api.saveSettings(payload);
      setEdits({});
      setFlash("Saved");
      setTimeout(() => setFlash(null), 3000);
      onSaved();
      await refresh();
    } catch (e) {
      setFlash(e instanceof ApiError ? e.message : "Could not save");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="space-y-10">
      <WritePosture setup={setup} onChange={(v) => void save(v)} />

      <TelegramPairing
        tokenPresent={setup.steps.telegram_token}
        paired={setup.steps.telegram_paired}
        onSave={(v) => void save(v)}
      />

      {GROUPS.map((g) => (
        <div key={g.title}>
          <h3 className="font-display text-lg text-ink">{g.title}</h3>
          <p className="mb-4 text-[13px] text-soft">{g.blurb}</p>
          <div className="space-y-3">
            {g.keys.map(({ k, label, hint }) => (
              <div key={k} className="flex items-center gap-3">
                <div className="w-48 shrink-0">
                  <p className="text-[14px] text-ink">{label}</p>
                  {hint && <p className="text-[11.5px] text-faint">{hint}</p>}
                </div>
                <input
                  type={PLAIN.has(k) || k.includes("ROOTS") ? "text" : "password"}
                  value={edits[k] ?? ""}
                  onChange={(e) => setEdits((s) => ({ ...s, [k]: e.target.value }))}
                  placeholder={present[k] ? values[k] || "saved" : "not set"}
                  className="min-w-0 flex-1 rounded-lg border border-rule bg-raised px-3 py-2 font-mono text-[13px] text-ink placeholder:text-faint focus:border-sepia focus:outline-none"
                />
                <span
                  className={`w-4 shrink-0 text-center ${present[k] ? "text-verdigris" : "text-faint"}`}
                  title={present[k] ? "set" : "not set"}
                >
                  {present[k] ? "●" : "○"}
                </span>
              </div>
            ))}
          </div>
        </div>
      ))}

      <div className="sticky bottom-6 flex items-center gap-3 rounded-xl border border-rule bg-raised/95 p-3 shadow-sheet backdrop-blur">
        <button
          onClick={() => void save()}
          disabled={saving || !Object.keys(edits).length}
          className="rounded-lg bg-sepia px-5 py-2 text-sm font-medium text-vellum transition hover:opacity-90 disabled:opacity-40"
        >
          {saving ? "Saving…" : "Save changes"}
        </button>
        <AnimatePresence>
          {flash && (
            <motion.span
              initial={{ opacity: 0, x: -6 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0 }}
              className="text-[13px] text-verdigris"
            >
              {flash}
            </motion.span>
          )}
        </AnimatePresence>
        <p className="ml-auto truncate font-mono text-[10px] text-faint" title={configPath}>
          {configPath}
        </p>
      </div>
    </section>
  );
}

/**
 * The two switches that decide whether anything reaches Notion, given their own block in
 * plain language. Burying "can this edit my notes" in a list of API keys is how someone
 * turns it on without meaning to.
 */
function WritePosture({
  setup,
  onChange,
}: {
  setup: SetupState;
  onChange: (v: Record<string, string>) => void;
}) {
  const levels = [
    { v: "none", label: "Ask me everything", d: "Nothing applies on its own." },
    {
      v: "low",
      label: "Citations only",
      d: "Adding a citation applies; anything that changes wording waits.",
    },
    {
      v: "medium",
      label: "Edits too",
      d: "Rewrites and merges apply. Contradictions wait.",
    },
    {
      v: "full",
      label: "Everything but disagreements",
      d: "All seven relations apply except contradictions, which wait for you.",
    },
    {
      v: "everything",
      label: "Disagreements too",
      d: "A conflicting source is written in beside the line it argues with — both sides, both cited. It never picks a winner, and Undo takes it back.",
    },
  ];

  return (
    <div className="rounded-xl border border-rule bg-raised p-5">
      <h3 className="font-display text-lg text-ink">What may it change on its own?</h3>
      <p className="mt-1 text-[13px] text-soft">
        Nothing here lets it decide which of two sources is right. Every change is
        listed in Activity with a way to take it back.
      </p>

      <label className="mt-4 flex cursor-pointer items-center gap-3">
        <input
          type="checkbox"
          checked={setup.apply}
          onChange={(e) => onChange({ PALIMPSEST_APPLY: e.target.checked ? "1" : "0" })}
          className="h-4 w-4 accent-[rgb(var(--sepia))]"
        />
        <span className="text-[14px] text-ink">Allow writing to Notion at all</span>
      </label>

      <div className="mt-4 grid gap-2">
        {levels.map((l) => {
          const active = setup.autonomy === l.v;
          return (
            <button
              key={l.v}
              disabled={!setup.apply}
              onClick={() => onChange({ PALIMPSEST_AUTONOMY: l.v })}
              className={[
                "relative rounded-lg border px-4 py-2.5 text-left transition disabled:opacity-40",
                active ? "border-sepia bg-sepia/5" : "border-rule hover:border-sepia/50",
              ].join(" ")}
            >
              {active && (
                <motion.span
                  layoutId="autonomy-marker"
                  className="absolute bottom-2 left-0 top-2 w-1 rounded-full bg-sepia"
                  transition={{ type: "spring", stiffness: 380, damping: 30 }}
                />
              )}
              <p className="pl-2 text-[14px] font-medium text-ink">{l.label}</p>
              <p className="pl-2 text-[12.5px] text-soft">{l.d}</p>
            </button>
          );
        })}
      </div>
    </div>
  );
}
