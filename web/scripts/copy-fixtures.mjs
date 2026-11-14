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
// The seeded demo customers (invented people and products) from ../customers.
const people = join(here, "..", "..", "customers");
mkdirSync(join(dst, "customers"), { recursive: true });
for (const f of readdirSync(people)) if (f.endsWith(".json")) cpSync(join(people, f), join(dst, "customers", f));
console.log(`fixtures copied to lib/fixtures`);
