/**
 * Copy the static export into the Python package, so `pip install palimpsest-notion`
 * ships a built UI and nobody needs Node at runtime.
 *
 * The old hand-written index.html is replaced wholesale — this is the UI now.
 */
import { cp, rm, mkdir, readdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const from = resolve(here, "..", "out");
const to = resolve(here, "..", "..", "..", "src", "palimpsest", "serve", "static");

await rm(to, { recursive: true, force: true });
await mkdir(to, { recursive: true });
await cp(from, to, { recursive: true });

const top = await readdir(to);
console.log(`shipped ${top.length} entries to ${to}`);
console.log(top.join("  "));
