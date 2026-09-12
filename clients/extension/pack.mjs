/**
 * Zip the extension into something a person can actually install.
 *
 *     node clients/extension/pack.mjs            # -> dist/palimpsest-extension-<version>.zip
 *
 * Until this existed the only way in was `chrome://extensions → Load unpacked → point at
 * this folder`, which is a developer instruction wearing a user's clothes: it needs the
 * repository checked out, it needs developer mode on, and the extension silently
 * disappears on some Chrome updates. A zip attached to the release is not the Web Store,
 * but it is a file you can drag onto the extensions page.
 *
 * Files are listed rather than globbed, for the same reason electron-builder's list is:
 * a glob of this directory sweeps in `test/`, its fixtures, and anything a development
 * session left lying about. An allowlist cannot ship what it was not told to.
 */

import { createWriteStream } from "node:fs";
import { mkdir, readFile, readdir, stat } from "node:fs/promises";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateRawSync } from "node:zlib";

const HERE = dirname(fileURLToPath(import.meta.url));

/** Everything the extension needs at runtime, and nothing else. */
const FILES = [
  "manifest.json",
  "background.js",
  "api.js",
  "router.js",
  "scrape.js",
  "popup.html",
  "popup.css",
  "popup.js",
  "options.html",
  "options.js",
];

/** Icon sizes the manifest declares. Missing one is a load-time error in Chrome. */
const ICON_DIR = "icons";

// ---------------------------------------------------------------------------
// a minimal zip writer
// ---------------------------------------------------------------------------
//
// Rather than a dependency. The extension has none at runtime and the repository has one
// devDependency for its tests; adding an archiver so that a release can produce a zip
// would be the largest dependency in the project, for forty lines of well-specified
// format. Deflate comes from `node:zlib`.

function crc32(buffer) {
  let crc = ~0;
  for (const byte of buffer) {
    crc ^= byte;
    for (let i = 0; i < 8; i += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return ~crc >>> 0;
}

function zip(entries) {
  const chunks = [];
  const central = [];
  let offset = 0;

  for (const { name, data } of entries) {
    const nameBytes = Buffer.from(name, "utf8");
    const deflated = deflateRawSync(data);
    // Only store the compressed form if it actually helped; a tiny JSON file often
    // deflates larger than it started, and a zip entry bigger than its content is a
    // silly thing to ship.
    const stored = deflated.length >= data.length;
    const body = stored ? data : deflated;

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);            // version needed
    local.writeUInt16LE(0, 6);             // flags
    local.writeUInt16LE(stored ? 0 : 8, 8); // 0 = store, 8 = deflate
    local.writeUInt32LE(0, 10);            // mtime/mdate: zeroed, see below
    local.writeUInt32LE(crc32(data), 14);
    local.writeUInt32LE(body.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    local.writeUInt16LE(0, 28);

    chunks.push(local, nameBytes, body);

    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0);
    entry.writeUInt16LE(20, 4);
    entry.writeUInt16LE(20, 6);
    entry.writeUInt16LE(0, 8);
    entry.writeUInt16LE(stored ? 0 : 8, 10);
    entry.writeUInt32LE(0, 12);
    entry.writeUInt32LE(crc32(data), 16);
    entry.writeUInt32LE(body.length, 20);
    entry.writeUInt32LE(data.length, 24);
    entry.writeUInt16LE(nameBytes.length, 28);
    entry.writeUInt32LE(0, 38);            // external attrs
    entry.writeUInt32LE(offset, 42);
    central.push(entry, nameBytes);

    offset += local.length + nameBytes.length + body.length;
  }

  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);

  return Buffer.concat([...chunks, directory, end]);
}

// ---------------------------------------------------------------------------

export async function pack(outDir = join(HERE, "dist")) {
  const manifest = JSON.parse(await readFile(join(HERE, "manifest.json"), "utf8"));
  const entries = [];

  for (const name of FILES) {
    const path = join(HERE, name);
    // A missing file must fail the build rather than produce a zip that fails to load
    // in somebody's browser with a message about a resource they have never heard of.
    await stat(path);
    entries.push({ name, data: await readFile(path) });
  }

  for (const icon of await readdir(join(HERE, ICON_DIR))) {
    entries.push({
      name: `${ICON_DIR}/${icon}`,
      data: await readFile(join(HERE, ICON_DIR, icon)),
    });
  }

  await mkdir(outDir, { recursive: true });
  const target = resolve(outDir, `palimpsest-extension-${manifest.version}.zip`);
  await new Promise((ok, fail) => {
    const stream = createWriteStream(target);
    stream.on("error", fail);
    stream.on("finish", ok);
    stream.end(zip(entries));
  });

  return { path: target, files: entries.length, version: manifest.version };
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) {
  const out = await pack();
  console.log(
    `packed ${out.files} files -> ${relative(process.cwd(), out.path)} (v${out.version})`,
  );
}
