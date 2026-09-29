"use strict";

// Browser smoke probe for one independently attested, static React fixture.
const { createHash } = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require("playwright-core");

const EXPECTED = {
  "index.html": "ee9a5bfac05c54185d82466c9caf34561d776660a34a102078558769e5100487",
  "app.js": "934df6615da70a35120da2e348d356d95089786717cb70880539cca5ea029ff2",
  "app.css": "f5b42a61a3bd14446cce2463ebf4657a7535ee928e191781a4f945d140ea53e1",
};
const PATHS = { "/": "index.html", "/app.js": "app.js", "/app.css": "app.css" };
const MEDIA = { "index.html": "text/html", "app.js": "text/javascript", "app.css": "text/css" };
const RED_OVERRIDE = "\n.primary-button{background-color:#ff0000!important}\n";

function requireCondition(condition, message) {
  if (!condition) throw new Error(message);
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function loadArtifacts(root) {
  requireCondition(fs.existsSync(root) && fs.statSync(root).isDirectory(), "missing artifact directory");
  const names = fs.readdirSync(root).sort();
  requireCondition(JSON.stringify(names) === JSON.stringify(Object.keys(EXPECTED).sort()), "unexpected artifact inventory");
  const files = {};
  for (const name of names) {
    const target = path.join(root, name);
    requireCondition(fs.lstatSync(target).isFile(), `artifact is not a regular file: ${name}`);
    files[name] = fs.readFileSync(target);
    requireCondition(sha256(files[name]) === EXPECTED[name], `artifact hash drift: ${name}`);
  }
  return files;
}

async function probe(browser, origin, files, inject) {
  const context = await browser.newContext({ serviceWorkers: "block" });
  const attempts = [];
  const responses = [];
  const errors = [];
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin || !(url.pathname in PATHS)) {
      attempts.push(route.request().url());
      await route.abort();
    } else if (inject && url.pathname === "/app.css") {
      await route.fulfill({
        status: 200,
        contentType: "text/css",
        body: Buffer.concat([files["app.css"], Buffer.from(RED_OVERRIDE)]),
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
    return {
      count: document.querySelectorAll("button.primary-button").length,
      visible: button.getBoundingClientRect().width > 0 && button.getBoundingClientRect().height > 0,
      background: getComputedStyle(button).backgroundColor,
      token: getComputedStyle(document.documentElement).getPropertyValue("--color-action-primary").trim(),
      root_children: document.getElementById("root").children.length,
      stylesheet: document.querySelector("link[rel=stylesheet]")?.getAttribute("href"),
    };
  });
  let screenshot = null;
  if (!inject) {
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

async function main() {
  requireCondition(process.argv.length === 3, "usage: node browser-verify.cjs OUTPUT_DIR");
  const files = loadArtifacts(path.resolve(process.argv[2]));
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
    const normal = await probe(browser, origin, files, false);
    const mutated = await probe(browser, origin, files, true);
    for (const result of [normal, mutated]) {
      requireCondition(result.attempts.length === 0, "browser attempted an external or unknown request");
      requireCondition(result.errors.length === 0, `browser errors: ${result.errors.join("; ")}`);
      requireCondition(result.observed.count === 1 && result.observed.visible && result.observed.root_children === 1, "mounted button missing or ambiguous");
      requireCondition(result.observed.token === "#2563eb" && result.observed.stylesheet === "./app.css", "generated token sheet was not loaded");
      requireCondition(["/", "/app.js", "/app.css"].every((name) => result.responses.some((item) => item.path === name && item.status === 200)), "browser did not load all attested assets");
    }
    assertApprovedStyle(normal);
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
    process.stdout.write(JSON.stringify({
      status: "pass",
      scope: "one mounted static React fixture and its button background probe",
      browser_version: browser.version(),
      normal: normal.observed,
      injected_css: { background: mutated.observed.background, rejected_by_approved_style_assertion: true },
      external_or_unknown_attempts: normal.attempts.length + mutated.attempts.length,
      served_paths: [...new Set(requests)].sort(),
      screenshot: normal.screenshot,
      artifact_sha256: EXPECTED,
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
