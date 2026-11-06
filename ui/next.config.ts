import { randomUUID } from "node:crypto";
import type { NextConfig } from "next";

// One id per build, shared with the client so the stored query cache can
// tell a cache written by an earlier build from its own.
const buildId = randomUUID();

const nextConfig: NextConfig = {
  output: "export",
  distDir: "build",
  images: {
    unoptimized: true,
  },
  env: {
    NEXT_PUBLIC_BUILD_ID: buildId,
  },
};

export default nextConfig;
