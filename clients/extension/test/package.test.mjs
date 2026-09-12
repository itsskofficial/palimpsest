/**
 * The extension has to be loadable, which is a different claim from "the code is right".
 *
 * Chrome validates a manifest at install time and refuses the whole extension for a
 * single missing file, with a message naming a resource the person installing it has
 * never heard of. The failure is total — no popup, no background worker, no clue — and
 * it happens on their machine rather than on ours.
 *
 * Every path the manifest names is therefore checked here against the files the packer
 * actually puts in the zip, rather than against the working directory. Those are two
 * different sets, and the one that matters is the one that ships: a file present on disk
 * but absent from the allowlist in `pack.mjs` passes every other kind of check and then
 * breaks for everybody who downloads it.
 */

import assert from "node:assert/strict";
import { readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { pack } from "../pack.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");

/** Read the zip back as a name -> Buffer map, without a dependency. */
async function unpack(path) {
  const buffer = await readFile(path);
  const files = new Map();
  const { inflateRawSync } = await import("node:zlib");

  let cursor = 0;
  while (cursor < buffer.length - 4 && buffer.readUInt32LE(cursor) === 0x04034b50) {
    const method = buffer.readUInt16LE(cursor + 8);
    const compressed = buffer.readUInt32LE(cursor + 18);
    const nameLength = buffer.readUInt16LE(cursor + 26);
    const extraLength = buffer.readUInt16LE(cursor + 28);
    const name = buffer.subarray(cursor + 30, cursor + 30 + nameLength).toString("utf8");
    const start = cursor + 30 + nameLength + extraLength;
    const body = buffer.subarray(start, start + compressed);
    files.set(name, method === 0 ? body : inflateRawSync(body));
    cursor = start + compressed;
  }
  return files;
}

async function packed() {
  const out = join(tmpdir(), `palimpsest-ext-${process.pid}`);
  const result = await pack(out);
  const files = await unpack(result.path);
  await rm(out, { recursive: true, force: true });
  return { files, version: result.version };
}

test("the zip contains every file the manifest names", async () => {
  const { files } = await packed();
  const manifest = JSON.parse(files.get("manifest.json").toString("utf8"));

  const referenced = new Set([
    manifest.background.service_worker,
    manifest.action.default_popup,
    manifest.options_page,
    ...Object.values(manifest.action.default_icon),
    ...Object.values(manifest.icons),
  ]);

  for (const path of referenced) {
    assert.ok(files.has(path), `manifest names ${path}, which is not in the zip`);
  }
});

test("every script the popup and options pages load is in the zip", async () => {
  const { files } = await packed();

  for (const page of ["popup.html", "options.html"]) {
    const html = files.get(page).toString("utf8");
    for (const [, src] of html.matchAll(/<script[^>]+src="([^"]+)"/g)) {
      assert.ok(files.has(src), `${page} loads ${src}, which is not in the zip`);
    }
    for (const [, href] of html.matchAll(/<link[^>]+href="([^"]+)"/g)) {
      assert.ok(files.has(href), `${page} links ${href}, which is not in the zip`);
    }
  }
});

test("every module an entry point imports is in the zip", async () => {
  // A service worker is a module, so a bare `import "./router.js"` that resolves on this
  // machine and is missing from the archive fails only after install.
  const { files } = await packed();

  for (const [name, data] of files) {
    if (!name.endsWith(".js")) continue;
    const source = data.toString("utf8");
    for (const [, spec] of source.matchAll(/from\s+"(\.\/[^"]+)"/g)) {
      const resolved = spec.replace(/^\.\//, "");
      assert.ok(files.has(resolved), `${name} imports ${spec}, which is not in the zip`);
    }
  }
});

test("nothing from the test directory is shipped", async () => {
  const { files } = await packed();

  for (const name of files.keys()) {
    assert.ok(!name.startsWith("test/"), `${name} should not be in a release`);
    assert.ok(!name.endsWith(".db"), `${name} should not be in a release`);
    assert.ok(!name.includes("node_modules"), `${name} should not be in a release`);
  }
});

test("the manifest is a manifest Chrome will accept", async () => {
  const { files } = await packed();
  const manifest = JSON.parse(files.get("manifest.json").toString("utf8"));

  assert.equal(manifest.manifest_version, 3);
  assert.match(manifest.version, /^\d+\.\d+(\.\d+)?$/);
  assert.ok(manifest.name && manifest.description);
  // Manifest V3 has no `background.scripts`, and mixing the two is a load error rather
  // than a warning.
  assert.equal(manifest.background.type, "module");
  assert.ok(!("scripts" in manifest.background));
  // The extension talks to a server on this machine, and to nothing else by default.
  assert.deepEqual(manifest.host_permissions,
                   ["http://127.0.0.1/*", "http://localhost/*"]);
});

test("the extension version matches the package it talks to", async () => {
  // Not cosmetic: the popup and the server agree on an API shape, and a user reporting
  // "the extension is broken" is much easier to help when the two numbers line up.
  const { version } = await packed();
  const python = await readFile(join(ROOT, "..", "..", "src", "palimpsest", "_version.py"),
                                "utf8");
  const match = python.match(/__version__ = "([^"]+)"/);

  assert.ok(match, "could not read the Python version");
  assert.equal(version, match[1],
               "bump clients/extension/manifest.json with the package");
});
