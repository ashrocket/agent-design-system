"use strict";

// A tool-owned recipe that builds reviewed, app-authored entry and host files.
const { createHash } = require("node:crypto");
const { realpathSync, readFileSync } = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const esbuild = require("esbuild");

const MAX_INPUT = 4096;
const MAX_OUTPUT = 4 * 1024 * 1024;

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

async function main() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT) throw new Error("build request exceeds limit");
    chunks.push(chunk);
  }
  const request = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!request || typeof request !== "object" || Array.isArray(request) ||
      Object.keys(request).join(",") !== "root" || typeof request.root !== "string" ||
      !path.isAbsolute(request.root)) throw new Error("invalid build request");
  const root = realpathSync(request.root);
  const entry = readFileSync(path.join(root, "main.tsx"));
  const host = readFileSync(path.join(root, "index.html"));
  const outputRoot = path.join(os.tmpdir(), "adsf-in-memory-output");
  const result = await esbuild.build({
    absWorkingDir: root,
    entryPoints: [path.join(root, "main.tsx")],
    bundle: true,
    packages: "bundle",
    platform: "browser",
    format: "esm",
    target: "es2020",
    jsx: "automatic",
    define: { "process.env.NODE_ENV": '"production"' },
    nodePaths: [path.join(__dirname, "node_modules")],
    outdir: outputRoot,
    entryNames: "app",
    write: false,
    metafile: true,
    sourcemap: false,
    minify: true,
    legalComments: "none",
    logLevel: "silent",
  });
  if (result.warnings.length) throw new Error("build emitted warnings");
  const files = [{ name: "index.html", bytes: host }];
  for (const file of result.outputFiles || []) {
    const name = path.basename(file.path);
    if (!file.path.startsWith(outputRoot + path.sep) || !["app.js", "app.css"].includes(name)) {
      throw new Error("unexpected build output path");
    }
    files.push({ name, bytes: Buffer.from(file.contents) });
  }
  if (files.length !== 3 || new Set(files.map((item) => item.name)).size !== 3) {
    throw new Error("build must emit exactly HTML, JS and CSS");
  }
  if (files.reduce((total, item) => total + item.bytes.length, 0) > MAX_OUTPUT) {
    throw new Error("build output exceeds limit");
  }
  const response = {
    protocol: "adsf-react-fixture-build-v1",
    builder_version: esbuild.version,
    react_version: require("react/package.json").version,
    react_dom_version: require("react-dom/package.json").version,
    entry_sha256: sha256(entry),
    host_sha256: sha256(host),
    metafile: result.metafile,
    files: files.map((item) => ({
      name: item.name,
      sha256: sha256(item.bytes),
      base64: item.bytes.toString("base64"),
    })),
  };
  process.stdout.write(JSON.stringify(response) + "\n");
}

main().catch((error) => {
  process.stderr.write(`ADSF rendered fixture build failed: ${error.message}\n`);
  process.exitCode = 2;
});
