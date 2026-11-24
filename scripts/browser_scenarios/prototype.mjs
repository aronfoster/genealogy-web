// Synthetic presentation uses the frozen route inventory, fonts and viewport driver.
import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";
import packaged from "@sparticuz/chromium";
import { artifacts } from "../artifacts.mjs";
import { openArtifact } from "../browser.mjs";
import { checkPrototype } from "../verify_prototype.mjs";
import { isAvailableRoute } from "../../prototype/route-policy.mjs";

const presentation = "scripts/browser_scenarios/presentation";
const directory = "scripts/browser_scenarios/captures";
const files = new Map([
  ["/", [presentation + "/index.html", "text/html"]],
  ["/app.js", [presentation + "/app.js", "text/javascript"]],
  ["/styles.css", ["prototype/styles.css", "text/css"]],
  ["/route-policy.mjs", ["prototype/route-policy.mjs", "text/javascript"]],
  ...[
    "inter-latin-normal",
    "source-serif-4-latin-normal",
    "source-serif-4-latin-italic",
  ].map((font) => [
    `/fonts/${font}.woff2`,
    [`prototype/fonts/${font}.woff2`, "font/woff2"],
  ]),
]);

async function withBrowser(run) {
  const server = createServer(async (request, res) => {
    const url = new URL(request.url, "http://localhost");
    const entry = files.get(url.pathname);
    if (!entry) {
      res.writeHead(404, { "content-type": "text/plain" });
      res.end("Not found");
      return;
    }
    try {
      const bytes = await readFile(entry[0]);
      res.writeHead(
        url.pathname === "/" && !isAvailableRoute(url.searchParams) ? 404 : 200,
        { "content-type": entry[1], "cache-control": "no-store" },
      );
      res.end(bytes);
    } catch {
      res.writeHead(500);
      res.end();
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    browser = await chromium.launch({
      executablePath: await packaged.executablePath(),
      args: [...packaged.args, "--disable-gpu", "--num-raster-threads=1"],
    });
    const context = await browser.newContext({
      locale: "en-US",
      timezoneId: "UTC",
      reducedMotion: "reduce",
      colorScheme: "light",
      deviceScaleFactor: 1,
    });
    await context.route("**/*", (route) =>
      new URL(route.request().url()).origin === base
        ? route.continue()
        : route.abort(),
    );
    await run({ context, base });
  } finally {
    await browser?.close();
    await new Promise((resolve) => server.close(resolve));
  }
}

async function sourceHash() {
  const hash = createHash("sha256");
  for (const path of [...files.values()]
    .map(([path]) => path)
    .concat([
      "scripts/artifacts.mjs",
      "scripts/browser.mjs",
      "scripts/browser_scenarios/prototype.mjs",
      presentation + "/expectations.json",
      "package-lock.json",
    ])) {
    hash.update(path).update(await readFile(path));
  }
  return hash.digest("hex");
}

if (process.argv.includes("--capture")) {
  await mkdir(directory, { recursive: true });
  const captures = {};
  await withBrowser(async ({ context, base }) => {
    const page = await context.newPage();
    for (const item of artifacts) {
      await openArtifact(page, base, item);
      const target = item.element ? page.locator(item.element) : page;
      const bytes = await target.screenshot({
        path: join(directory, item.name),
        animations: "disabled",
        ...(item.element ? {} : { fullPage: true }),
      });
      captures[item.name] = createHash("sha256").update(bytes).digest("hex");
    }
  });
  await writeFile(
    join(directory, "manifest.json"),
    JSON.stringify({ sourceHash: await sourceHash(), captures }, null, 2) +
      "\n",
  );
} else {
  await checkPrototype({
    directory,
    specPath: "scripts/browser_scenarios/README.md",
    verifyRenders: process.argv.includes("--visual"),
    sourceHash,
    withBrowser,
    openArtifact,
    expectedPath: presentation + "/expectations.json",
  });
}
