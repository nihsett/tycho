import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

// Vitest rewrites `import.meta.url` to a server-relative path, so it is not a
// filesystem anchor here. The runner's working directory is the package root.
const SRC = join(process.cwd(), "src");
const LIB = join(SRC, "lib");

/**
 * Source-level safety assertions.
 *
 * These belong here and not in a check over the built bundle: React's own
 * runtime names `dangerouslySetInnerHTML` internally, so the string is always
 * present once React is bundled. Whether the *application* uses it is a
 * question about this directory.
 */
function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      found.push(...sourceFiles(path));
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.test\.(ts|tsx)$/.test(entry)) {
      found.push(path);
    }
  }
  return found;
}

const FILES = sourceFiles(SRC);

/** Strip block and line comments so a prose mention is not a false positive. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

describe("frontend source safety", () => {
  it("finds the application source", () => {
    expect(FILES.length).toBeGreaterThan(10);
  });

  it.each([
    "dangerouslySetInnerHTML",
    ".innerHTML",
    "outerHTML",
    "document.write",
    "eval(",
    "new Function(",
  ])("never uses %s", (pattern) => {
    for (const file of FILES) {
      expect(code(readFileSync(file, "utf8")), file).not.toContain(pattern);
    }
  });

  it("embeds no credential, project id, or Google API endpoint", () => {
    for (const file of FILES) {
      const text = readFileSync(file, "utf8");
      for (const marker of [
        "AIza",
        "BEGIN PRIVATE KEY",
        "client_secret",
        "service_account",
        "googleapis.com",
        "gen-lang-client-",
      ]) {
        expect(text, `${file} contains ${marker}`).not.toContain(marker);
      }
    }
  });

  it("talks only to same-origin /api paths", () => {
    const client = readFileSync(join(LIB, "api.ts"), "utf8");
    expect(client).toContain('credentials: "same-origin"');
    const urls = client.match(/"https?:\/\/[^"]+"/g) ?? [];
    expect(urls).toEqual([]);
  });
});
