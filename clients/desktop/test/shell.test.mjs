/**
 * The desktop shell, checked without launching Electron.
 *
 * This file existed at zero tests while being the thing most people will actually run.
 * Most of it cannot be exercised outside a real Electron process — windows, trays,
 * global shortcuts — but the parts that break installs can, and they are exactly the
 * parts nobody notices until someone else's machine.
 *
 * Two failures in particular are worth guarding, because both are invisible here and
 * total there:
 *
 * 1. **A file main.js needs is not in the installer's allowlist.** `electron-builder.yml`
 *    lists files explicitly, on purpose — a glob of this directory would sweep in a
 *    development SQLite database, which is somebody's actual notes shipped inside an
 *    installer. The cost of an allowlist is that adding a file to the app and forgetting
 *    the list produces an installer that starts and then fails on a resource the user
 *    has never heard of.
 *
 * 2. **The config path stops matching Python's.** The shell has to answer "where is the
 *    config" before it has a Python process to ask, so the rule is written twice in two
 *    languages. Duplicated logic that drifts silently means the app writes settings the
 *    engine never reads, and the wizard reappears on every launch.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFile, readdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { findPython } from "../backend.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = join(HERE, "..");
const REPO = join(APP, "..", "..");

async function builderFiles() {
  // A tiny reader rather than a YAML dependency: the `files:` block is a flat list of
  // `  - name` lines, and this only has to understand that shape.
  const yaml = await readFile(join(APP, "electron-builder.yml"), "utf8");
  const lines = yaml.split("\n");
  const start = lines.findIndex((l) => l.trim() === "files:");
  assert.ok(start >= 0, "electron-builder.yml has no files: block");

  const out = [];
  for (const line of lines.slice(start + 1)) {
    const match = line.match(/^\s+-\s+(\S+)\s*$/);
    if (!match) break;
    out.push(match[1]);
  }
  return out;
}

test("every file the shell loads at runtime is in the installer", async () => {
  const shipped = new Set(await builderFiles());
  const source = await readFile(join(APP, "main.js"), "utf8");

  const referenced = new Set(["main.js"]);
  for (const [, name] of source.matchAll(/join\(HERE,\s*"([^"]+)"\)/g)) {
    referenced.add(name);
  }
  for (const [, spec] of source.matchAll(/from\s+"\.\/([^"]+)"/g)) {
    referenced.add(spec);
  }

  for (const name of referenced) {
    assert.ok(shipped.has(name),
              `main.js loads ${name}, which electron-builder.yml does not ship`);
  }
});

test("every file the capture window loads is in the installer", async () => {
  const shipped = new Set(await builderFiles());
  const html = await readFile(join(APP, "capture.html"), "utf8");

  for (const [, src] of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
    if (src.startsWith("http") || src.startsWith("data:")) continue;
    assert.ok(shipped.has(src),
              `capture.html loads ${src}, which electron-builder.yml does not ship`);
  }
});

test("the installer ships nothing it should not", async () => {
  // The allowlist exists because a development run leaves a SQLite database in this
  // directory. Shipping one would put somebody's notes inside an installer.
  for (const name of await builderFiles()) {
    assert.ok(!name.endsWith(".db"), `${name} must never be packaged`);
    assert.ok(!name.includes("node_modules"), `${name} must never be packaged`);
    assert.ok(!name.startsWith("test"), `${name} must never be packaged`);
  }

  const present = await readdir(APP);
  assert.ok(present.includes("electron-builder.yml"));
});

test("the shell version matches the package it installs", async () => {
  const pkg = JSON.parse(await readFile(join(APP, "package.json"), "utf8"));
  const python = await readFile(join(REPO, "src", "palimpsest", "_version.py"), "utf8");
  const match = python.match(/__version__ = "([^"]+)"/);

  assert.ok(match, "could not read the Python version");
  assert.equal(pkg.version, match[1],
               "bump clients/desktop/package.json with the package");
});

test("the package.json is one electron-builder can package for Linux", async () => {
  // A bare string author satisfies Windows and macOS and fails the Linux target with
  // "Please specify author 'email'". Only the AppImage broke, so it went unnoticed
  // through a whole release.
  const pkg = JSON.parse(await readFile(join(APP, "package.json"), "utf8"));

  assert.equal(typeof pkg.author, "object", "author must be {name, email}");
  assert.ok(pkg.author.name, "author.name is required");
  assert.match(pkg.author.email ?? "", /@/, "author.email is required by the Linux build");
});

test("the config path agrees with the one Python computes", async () => {
  // Written twice because the shell must answer this before it has a Python to ask. If
  // the two ever disagree the app saves settings the engine never reads, and the setup
  // wizard reappears on every launch with no explanation.
  const source = await readFile(join(APP, "main.js"), "utf8");
  assert.match(source, /palimpsest["'],\s*["']config\.env/,
               "main.js should build <appData>/palimpsest/config.env");

  const probe = spawnSync(
    process.platform === "win32" ? "py" : "python3",
    ["-c",
     "import sys; sys.path.insert(0, 'src');" +
     "from palimpsest.config import config_path; print(config_path())"],
    { cwd: REPO, encoding: "utf8" },
  );

  if (probe.status !== 0) return;   // no interpreter here; the assertion above still held
  assert.match(probe.stdout.trim(), /palimpsest[\\/]config\.env$/);
});

test("findPython either finds a usable interpreter or says nothing was found", () => {
  // It must never return something unusable. A wrong answer here spawns a venv build
  // against a Python too old to run the package, which fails minutes later inside pip
  // with an error about a syntax feature rather than about the version.
  const found = findPython();

  if (found === null) return;
  assert.ok(Array.isArray(found.prefix), "findPython returns {command, prefix, version}");
  assert.match(found.version, /^3\.\d+$/);
  const probe = spawnSync(found.command,
                          [...found.prefix, "-c",
                           "import sys; print(sys.version_info[0])"],
                          { encoding: "utf8" });
  assert.equal(probe.status, 0, "findPython returned something that does not run");
  assert.equal(probe.stdout.trim(), "3");
});

// ---------------------------------------------------------------------------
// the first-run failure nobody has ever seen
// ---------------------------------------------------------------------------

test("with no usable Python the error names the version and what to do", async () => {
  // The single most likely way a first run fails, and the one nobody experiences while
  // developing — every machine that builds this app has Python on it. Left to the raw
  // failure it surfaces as `spawn ENOENT`, which reads like a bug in the app rather than
  // a missing dependency, so the message has to carry the version, where to get it, and
  // the Windows installer checkbox that is the actual cause more often than not.
  const source = await readFile(join(APP, "backend.js"), "utf8");
  const message = source.slice(source.indexOf("if (!python)"), source.indexOf("if (!python)") + 600);

  assert.match(message, /needs Python/, "it must say Python is the problem");
  assert.match(message, /MIN_PYTHON/, "and which version");
  assert.match(message, /python\.org/, "and where to get it");
  assert.match(message, /PATH/, "and the reason it is not found even when installed");
});

test("the minimum Python is one the package actually supports", async () => {
  // The shell refuses anything older, so a mismatch here either blocks an interpreter
  // that would have worked or accepts one that fails minutes later inside pip, with an
  // error about a syntax feature rather than about the version.
  const source = await readFile(join(APP, "backend.js"), "utf8");
  const shell = source.match(/MIN_PYTHON = \[(\d+), (\d+)\]/);
  assert.ok(shell, "MIN_PYTHON is not declared as a pair");

  const pyproject = await readFile(join(REPO, "pyproject.toml"), "utf8");
  const declared = pyproject.match(/requires-python\s*=\s*"[^0-9]*(\d+)\.(\d+)/);
  assert.ok(declared, "pyproject does not declare requires-python");

  assert.equal(`${shell[1]}.${shell[2]}`, `${declared[1]}.${declared[2]}`,
               "clients/desktop/backend.js and pyproject.toml disagree on the minimum");
});

test("the extras the shell installs are extras the package defines", async () => {
  // A typo here fails at `pip install` on a stranger's first run, several minutes in,
  // with pip's own message about an unknown extra.
  const source = await readFile(join(APP, "backend.js"), "utf8");
  const extras = source.match(/EXTRAS = "\[([^\]]+)\]"/);
  assert.ok(extras, "EXTRAS is not declared");

  const pyproject = await readFile(join(REPO, "pyproject.toml"), "utf8");
  const block = pyproject.slice(pyproject.indexOf("[project.optional-dependencies]"));
  for (const extra of extras[1].split(",").map((s) => s.trim())) {
    // `\\s` because this is a template literal: a single backslash there is swallowed,
    // and the regex silently becomes `^anthropics*=`, which matches nothing.
    assert.ok(new RegExp(`^${extra}\\s*=`, "m").test(block),
              `the shell installs [${extra}], which pyproject does not define`);
  }
});

test("the shell installs the distribution that exists on PyPI", async () => {
  // `palimpsest` is a different project. Getting this wrong installs somebody else's
  // package into the app's virtualenv and then fails on an import.
  const source = await readFile(join(APP, "backend.js"), "utf8");
  assert.match(source, /PACKAGE = "palimpsest-notion"/);

  const pyproject = await readFile(join(REPO, "pyproject.toml"), "utf8");
  assert.match(pyproject, /^name = "palimpsest-notion"$/m);
});
