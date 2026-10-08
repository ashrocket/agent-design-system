"use strict";

// Browser smoke probe for one independently attested, app-authored React page.
const { createHash } = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright-core");

const CONTRACT_SHA256 = "dc462ea8b33834870ede719fc01c74ee5693da3f0c141355cb0cadbc620b9f2a";
const HOST_SHA256 = "ee9a5bfac05c54185d82466c9caf34561d776660a34a102078558769e5100487";
const ENTRY_SHA256 = "72da1443db0148098b5454fc2ff659c609f46508294e116f5e63401f93e900c1";
const BUILDER_SHA256 = "7b1ba44ded6fb4eb87958da3df1245a44b68d6c1ddc455911fc3c0ca0c19e5ad";
const SOURCE_NAMES = ["index.html", "main.tsx", "App.tsx", "tokens.css", "layout.css", "components/button.css"];
const PATHS = { "/": "index.html", "/app.js": "app.js", "/app.css": "app.css" };
const MEDIA = { "index.html": "text/html", "app.js": "text/javascript", "app.css": "text/css" };
const RED_OVERRIDE = "\n.primary-button{background-color:#ff0000!important}\n";
const GENERATED_COPY_OVERRIDE = '\n.primary-button::after{content:" SALE"}\n';

function requireCondition(condition, message) {
  if (!condition) throw new Error(message);
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function loadAttestation(file) {
  const attestation = JSON.parse(fs.readFileSync(file, "utf8"));
  requireCondition(JSON.stringify(Object.keys(attestation).sort()) === JSON.stringify([
    "format", "contract_sha256", "host_sha256", "entry_sha256", "source_sha256", "artifact_sha256", "builder_sha256", "browser_verifier_sha256"
  ].sort()), "invalid attestation envelope");
  requireCondition(attestation.format === "adsf-rendered-app-attestation-v1", "wrong attestation protocol");
  requireCondition(attestation.contract_sha256 === CONTRACT_SHA256, "unreviewed contract attestation");
  requireCondition(attestation.host_sha256 === HOST_SHA256 && attestation.entry_sha256 === ENTRY_SHA256, "unreviewed app entry or host");
  requireCondition(attestation.builder_sha256 === BUILDER_SHA256, "unreviewed builder attestation");
  requireCondition(attestation.browser_verifier_sha256 === sha256(fs.readFileSync(__filename)), "browser verifier differs from attested script");
  const sources = attestation.source_sha256;
  requireCondition(sources && JSON.stringify(Object.keys(sources).sort()) === JSON.stringify(SOURCE_NAMES.sort()), "invalid source inventory in attestation");
  requireCondition(Object.values(sources).every((value) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value)), "invalid source hash in attestation");
  requireCondition(sources["index.html"] === HOST_SHA256 && sources["main.tsx"] === ENTRY_SHA256, "source inventory differs from reviewed app entry or host");
  const expected = attestation.artifact_sha256;
  requireCondition(expected && JSON.stringify(Object.keys(expected).sort()) === JSON.stringify(["index.html", "app.js", "app.css"].sort()), "invalid artifact inventory in attestation");
  requireCondition(Object.values(expected).every((value) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value)), "invalid artifact hash in attestation");
  requireCondition(expected["index.html"] === HOST_SHA256, "attested HTML differs from reviewed host");
  return expected;
}

function loadArtifacts(root, expected) {
  requireCondition(fs.existsSync(root) && fs.statSync(root).isDirectory(), "missing artifact directory");
  const names = fs.readdirSync(root).sort();
  requireCondition(JSON.stringify(names) === JSON.stringify(Object.keys(expected).sort()), "unexpected artifact inventory");
  const files = {};
  for (const name of names) {
    const target = path.join(root, name);
    requireCondition(fs.lstatSync(target).isFile(), `artifact is not a regular file: ${name}`);
    files[name] = fs.readFileSync(target);
    requireCondition(sha256(files[name]) === expected[name], `artifact hash drift: ${name}`);
  }
  return files;
}

async function probe(browser, origin, files, injectedCss) {
  const context = await browser.newContext({ serviceWorkers: "block" });
  const attempts = [];
  const responses = [];
  const errors = [];
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin || !(url.pathname in PATHS)) {
      attempts.push(route.request().url());
      await route.abort();
    } else if (injectedCss && url.pathname === "/app.css") {
      await route.fulfill({
        status: 200,
        contentType: "text/css",
        body: Buffer.concat([files["app.css"], Buffer.from(injectedCss)]),
      });
    } else {
      await route.continue();
    }
  });
  const page = await context.newPage();
  page.on("response", (response) => responses.push({path: new URL(response.url()).pathname, status: response.status()}));
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
  await page.goto(origin + "/", { waitUntil: "networkidle" });
  await page.locator("button.primary-button").waitFor({ state: "visible" });
  const observed = await page.evaluate(() => {
    const button = document.querySelector("button.primary-button");
    const generatedContent = [];
    for (const element of document.querySelectorAll("*")) {
      for (const pseudo of ["::before", "::after"]) {
        const content = getComputedStyle(element, pseudo).content;
        if (content && content !== "none" && content !== "normal") {
          generatedContent.push({ tag: element.tagName.toLowerCase(), pseudo, content });
        }
      }
    }
    return {
      count: document.querySelectorAll("button.primary-button").length,
      visible: button.getBoundingClientRect().width > 0 && button.getBoundingClientRect().height > 0,
      background: getComputedStyle(button).backgroundColor,
      foreground: getComputedStyle(button).color,
      padding_top: getComputedStyle(button).paddingTop,
      padding_bottom: getComputedStyle(button).paddingBottom,
      text: button.textContent,
      token: getComputedStyle(document.documentElement).getPropertyValue("--color-action-primary").trim(),
      root_children: document.getElementById("root").children.length,
      stylesheet: document.querySelector("link[rel=stylesheet]")?.getAttribute("href"),
      generated_content: generatedContent,
    };
  });
  let screenshot = null;
  if (!injectedCss) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "adsf-rendered-browser-"));
    const screenshotPath = path.join(directory, "fixture.png");
    const png = await page.screenshot({ path: screenshotPath, fullPage: true });
    screenshot = { path: screenshotPath, sha256: sha256(png) };
  }
  await context.close();
  return { observed, attempts, responses, errors, screenshot };
}

function assertApprovedStyle(result) {
  requireCondition(result.observed.background === "rgb(37, 99, 235)", "approved button color did not render");
}

function assertNoGeneratedContent(result) {
  requireCondition(result.observed.generated_content.length === 0, "generated CSS pseudo content detected");
}

async function main() {
  requireCondition(process.argv.length === 4 || (process.argv.length === 5 && process.argv[4] === "--expect-approved-edit-v1"), "usage: node browser-verify.cjs OUTPUT_DIR ATTESTATION_JSON [--expect-approved-edit-v1]");
  const expected = loadAttestation(path.resolve(process.argv[3]));
  const files = loadArtifacts(path.resolve(process.argv[2]), expected);
  const requests = [];
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://127.0.0.1").pathname;
    requests.push(pathname);
    const name = PATHS[pathname];
    if (!name || request.method !== "GET") {
      response.writeHead(404).end();
      return;
    }
    response.writeHead(200, {
      "Content-Type": MEDIA[name] + "; charset=utf-8",
      "Content-Length": files[name].length,
      "Cache-Control": "no-store",
      "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'none'; img-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'",
    }).end(files[name]);
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    const executablePath = process.env.ADSF_CHROME_BIN || undefined;
    browser = await chromium.launch({ executablePath, headless: true });
    const normal = await probe(browser, origin, files, null);
    const mutated = await probe(browser, origin, files, RED_OVERRIDE);
    const generated = await probe(browser, origin, files, GENERATED_COPY_OVERRIDE);
    for (const result of [normal, mutated, generated]) {
      requireCondition(result.attempts.length === 0, "browser attempted an external or unknown request");
      requireCondition(result.errors.length === 0, `browser errors: ${result.errors.join("; ")}`);
      requireCondition(result.observed.count === 1 && result.observed.visible && result.observed.root_children === 1, "mounted button missing or ambiguous");
      requireCondition(result.observed.token === "#2563eb" && result.observed.stylesheet === "./app.css", "generated token sheet was not loaded");
      requireCondition(["/", "/app.js", "/app.css"].every((name) => result.responses.some((item) => item.path === name && item.status === 200)), "browser did not load all attested assets");
    }
    assertApprovedStyle(normal);
    assertNoGeneratedContent(normal);
    assertNoGeneratedContent(mutated);
    requireCondition(normal.observed.foreground === "rgb(248, 250, 252)", "approved button foreground did not render");
    if (process.argv[4] === "--expect-approved-edit-v1") {
      requireCondition(normal.observed.text === "Review now", "approved edit text did not render");
      requireCondition(normal.observed.padding_top === "16px" && normal.observed.padding_bottom === "16px", "approved edit vertical padding did not render");
    }
    requireCondition(mutated.observed.background === "rgb(255, 0, 0)", "CSS-response mutation did not reach the browser probe");
    requireCondition(mutated.observed.background !== normal.observed.background, "browser probe missed the CSS-response mutation");
    let rejectedMutation = false;
    try {
      assertApprovedStyle(mutated);
    } catch (error) {
      requireCondition(error.message === "approved button color did not render", "mutation failed for an unrelated reason");
      rejectedMutation = true;
    }
    requireCondition(rejectedMutation, "mutated browser response unexpectedly passed the approved-style assertion");
    requireCondition(
      generated.observed.generated_content.some((item) => item.tag === "button" && item.pseudo === "::after"),
      "generated-content mutation did not reach the browser probe"
    );
    let rejectedGeneratedCopy = false;
    try {
      assertNoGeneratedContent(generated);
    } catch (error) {
      requireCondition(error.message === "generated CSS pseudo content detected", "generated-content mutation failed for an unrelated reason");
      rejectedGeneratedCopy = true;
    }
    requireCondition(rejectedGeneratedCopy, "generated-content mutation unexpectedly passed the visible-copy assertion");
    process.stdout.write(JSON.stringify({
      status: "pass",
      scope: "one mounted static authored React app with button color and generated-copy probes",
      browser_version: browser.version(),
      normal: normal.observed,
      injected_css: { background: mutated.observed.background, rejected_by_approved_style_assertion: true },
      generated_content_mutation: { observed: true, rejected_by_generated_content_assertion: true },
      external_or_unknown_attempts: normal.attempts.length + mutated.attempts.length + generated.attempts.length,
      served_paths: [...new Set(requests)].sort(),
      screenshot: normal.screenshot,
      artifact_sha256: expected,
    }, null, 2) + "\n");
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((error) => {
  process.stderr.write(`ADSF browser fixture failed: ${error.message}\n`);
  process.exitCode = 2;
});
