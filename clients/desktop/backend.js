/**
 * Finding a Python, building a venv in it, and keeping `palimpsest serve` alive.
 *
 * The app is a capture surface in front of a Python service. That service is not
 * bundled — it is installed into a virtualenv under the app's data directory using the
 * Python already on the machine. The trade is deliberate: a bundled runtime would make
 * the installer work anywhere, and it would also mean this app ships a *second* copy of
 * palimpsest that drifts from the one you edit and run from the terminal. Sharing one
 * checkout means a change you make is live in the desktop app the next time it starts.
 *
 * Three failure modes get explicit handling, because all three are common and all three
 * look identical from the outside if you do not:
 *
 * 1. **No Python.** Say so, name the version needed, and stop — rather than emitting a
 *    spawn ENOENT that reads like a bug in the app.
 * 2. **The port is already taken**, usually by a `palimpsest serve` in a terminal. That
 *    is a perfectly good server, so adopt it instead of fighting it.
 * 3. **The server dies.** Restart it, with a backoff, and give up loudly after a few
 *    tries instead of hammering a broken install forever.
 */

import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

const IS_WINDOWS = process.platform === "win32";
const BIN = IS_WINDOWS ? "Scripts" : "bin";
const EXE = IS_WINDOWS ? ".exe" : "";

/** Candidate interpreters, best first. `py -3` is the Windows launcher. */
const CANDIDATES = IS_WINDOWS
  ? [["py", ["-3"]], ["python", []], ["python3", []]]
  : [["python3", []], ["python", []]];

const MIN_PYTHON = [3, 10];

export function findPython() {
  for (const [command, prefix] of CANDIDATES) {
    const probe = spawnSync(command, [...prefix, "-c", "import sys;print('%d.%d'%sys.version_info[:2])"], {
      encoding: "utf8",
      windowsHide: true,
    });
    if (probe.status !== 0 || !probe.stdout) continue;
    const [major, minor] = probe.stdout.trim().split(".").map(Number);
    if (major > MIN_PYTHON[0] || (major === MIN_PYTHON[0] && minor >= MIN_PYTHON[1])) {
      return { command, prefix, version: `${major}.${minor}` };
    }
  }
  return null;
}

export class Backend {
  /**
   * @param {object} options
   * @param {string} options.venvDir   where the virtualenv lives
   * @param {string} options.repoRoot  the palimpsest checkout to install from
   * @param {number} options.port
   * @param {(line: string, level?: string) => void} options.log
   */
  constructor({ venvDir, repoRoot, port = 8100, env = {}, log = () => {} }) {
    this.venvDir = venvDir;
    this.repoRoot = repoRoot;
    this.port = port;
    this.env = env;
    this.log = log;
    this.child = null;
    this.adopted = false;
    this.restarts = 0;
    this.stopping = false;
  }

  get url() {
    return `http://127.0.0.1:${this.port}`;
  }

  get python() {
    return join(this.venvDir, BIN, `python${EXE}`);
  }

  get installed() {
    return existsSync(this.python);
  }

  async alive() {
    try {
      const response = await fetch(`${this.url}/healthz`, {
        signal: AbortSignal.timeout(1500),
      });
      return response.ok;
    } catch {
      return false;
    }
  }

  /** Create the venv and install palimpsest into it. Slow, and only ever once. */
  async install(onProgress = () => {}) {
    const python = findPython();
    if (!python) {
      throw new Error(
        `palimpsest needs Python ${MIN_PYTHON.join(".")} or newer, and none was found ` +
          `on your PATH. Install it from python.org, tick "Add python.exe to PATH", ` +
          `then reopen this app.`,
      );
    }

    if (!existsSync(this.python)) {
      onProgress(`Creating a virtualenv with Python ${python.version}…`);
      await this.#run(python.command, [...python.prefix, "-m", "venv", this.venvDir]);
    }

    onProgress("Installing palimpsest and its dependencies…");
    // `-e` so the app runs the checkout you edit rather than a frozen copy.
    await this.#run(this.python, [
      "-m", "pip", "install", "--disable-pip-version-check", "-q",
      "-e", `${this.repoRoot}[anthropic,serve,pdf,tabular]`,
    ]);
    onProgress("Ready.");
  }

  #run(command, args) {
    return new Promise((resolve, reject) => {
      this.log(`$ ${command} ${args.join(" ")}`);
      const child = spawn(command, args, { windowsHide: true, env: { ...process.env } });
      let stderr = "";
      child.stderr.on("data", (d) => {
        stderr += d;
        this.log(String(d).trimEnd());
      });
      child.stdout.on("data", (d) => this.log(String(d).trimEnd()));
      child.on("error", reject);
      child.on("close", (code) =>
        code === 0
          ? resolve()
          : reject(new Error(`${command} exited with ${code}\n${stderr.slice(-2000)}`)),
      );
    });
  }

  async start(onProgress = () => {}) {
    // Someone already serving on our port is a feature, not a conflict: it is almost
    // always a `palimpsest serve` running in a terminal, and two servers on one SQLite
    // file is a worse outcome than sharing one.
    if (await this.alive()) {
      this.adopted = true;
      this.log(`adopted an existing server on ${this.url}`);
      return { adopted: true, url: this.url };
    }

    if (!this.installed) await this.install(onProgress);

    onProgress("Starting the server…");
    this.#spawnServer();

    // Poll rather than trusting the process to be listening the moment it exists.
    for (let attempt = 0; attempt < 60; attempt += 1) {
      if (await this.alive()) {
        this.log(`server up on ${this.url}`);
        return { adopted: false, url: this.url };
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    throw new Error(
      `The server did not become healthy on ${this.url} within 30 seconds. ` +
        `Check the log window for what it printed.`,
    );
  }

  #spawnServer() {
    const args = ["-m", "palimpsest.cli", "serve", "--port", String(this.port)];
    this.child = spawn(this.python, args, {
      windowsHide: true,
      env: {
        ...process.env,
        ...this.env,
        PYTHONUNBUFFERED: "1",
        PALIMPSEST_HOST: "127.0.0.1",
      },
    });
    this.child.stdout.on("data", (d) => this.log(String(d).trimEnd()));
    this.child.stderr.on("data", (d) => this.log(String(d).trimEnd(), "warn"));
    this.child.on("close", (code) => {
      this.child = null;
      if (this.stopping) return;
      if (this.restarts >= 3) {
        this.log(`server exited (${code}) and has failed 3 times; not restarting`, "error");
        return;
      }
      this.restarts += 1;
      const wait = 1000 * this.restarts;
      this.log(`server exited (${code}); restarting in ${wait}ms`, "warn");
      setTimeout(() => !this.stopping && this.#spawnServer(), wait);
    });
  }

  stop() {
    this.stopping = true;
    // A server we adopted belongs to someone else's terminal; killing it on quit would
    // be a genuinely surprising thing for this app to do.
    if (this.child && !this.adopted) this.child.kill();
    this.child = null;
  }
}
