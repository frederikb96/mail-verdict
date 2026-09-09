/**
 * Resolves the app's `@/*` bundler alias (see tsconfig.json's `paths`) to
 * `src/*` when unit tests run under Node's own `--test` runner -- webpack
 * and tsc already understand the alias natively, but a bare `node`
 * process has no bundler in front of it to do that translation.
 */
import path from "node:path";
import { pathToFileURL } from "node:url";

const SRC_URL = pathToFileURL(`${path.resolve(import.meta.dirname, "../src")}/`).href;

export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith("@/")) {
    return nextResolve(`${SRC_URL}${specifier.slice(2)}.ts`, context);
  }
  return nextResolve(specifier, context);
}
