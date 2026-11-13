// Copies the shared contract fixtures (../fixtures/api) into lib/fixtures for mock mode.
// The originals stay untouched; lib/fixtures is gitignored and refreshed on every dev/build.
import { cpSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, "..", "..", "fixtures", "api");
const dst = join(here, "..", "lib", "fixtures");
mkdirSync(dst, { recursive: true });
for (const f of readdirSync(src)) if (f.endsWith(".json")) cpSync(join(src, f), join(dst, f));
console.log(`fixtures copied to lib/fixtures`);
