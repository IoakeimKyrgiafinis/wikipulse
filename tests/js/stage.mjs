// Copies the site's ES modules into a temporary folder marked {"type": "module"}, so any
// Node version loads them as modules (site/ has no package.json, by design: no build
// step). Used by the other scripts in tests/js/.
import { cpSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

export function stage(siteDir, { vendor = false } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "wikipulse-site-"));
  process.on("exit", () => rmSync(dir, { recursive: true, force: true }));
  writeFileSync(join(dir, "package.json"), '{"type": "module"}\n');
  for (const name of readdirSync(siteDir)) {
    if (name.endsWith(".js")) cpSync(join(siteDir, name), join(dir, name));
  }
  if (vendor) cpSync(join(siteDir, "vendor"), join(dir, "vendor"), { recursive: true });
  return dir;
}

export function importFrom(dir, name) {
  return import(pathToFileURL(join(dir, name)).href);
}
