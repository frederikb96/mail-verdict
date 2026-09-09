// Copies pdf.js's worker from the installed package into `public/` so it is
// served as a real same-origin static file rather than reached through the
// bundler-relative `new URL(...)` pattern, which fails in a Next.js static
// export, or pdf.js's own blob-based fallback worker, which needs a CSP
// grant this application does not have. Runs before every dev server start
// and build so the copy always matches the installed dependency's version --
// it is never committed.
import { copyFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const workerSrc = require.resolve("pdfjs-dist/build/pdf.worker.min.mjs");
copyFileSync(workerSrc, new URL("../public/pdf.worker.min.mjs", import.meta.url));
