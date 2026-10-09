import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";

const ROOT = path.join(import.meta.dirname, "..", "docs");

/** docs/customer and docs/shared as {"customer/x.md": text}, inlined at build
 *  so the server bundle carries them; empty where docs/ is not checked out. */
export function docsBundle() {
  const files = {};
  for (const folder of ["customer", "shared"]) {
    const dir = path.join(ROOT, folder);
    if (!existsSync(dir)) continue;
    for (const name of readdirSync(dir, { recursive: true })) {
      if (/\.(md|json)$/.test(name) && name !== "AGENTS.md")
        files[`${folder}/${name}`] = readFileSync(path.join(dir, name), "utf8");
    }
  }
  return JSON.stringify(files);
}
