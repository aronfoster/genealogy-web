import { manualListingOnly } from "./check_navigation.mjs";
import assert from "node:assert/strict";
import { accessibility, screenshot } from "./browser_checks.mjs";
import { mkdir } from "node:fs/promises";

export async function checkSourceFilters(
  context,
  page,
  base,
  family,
  expected,
) {
  const ready = () =>
    page.waitForFunction(() => {
      const list = document.querySelector("[data-listing]");
      return (
        list &&
        !list.querySelector("[data-listing-controls]").hidden &&
        !list.classList.contains("listing-restoring") &&
        !document.documentElement.classList.contains("family-revalidating")
      );
    });
  const count = () => page.locator(".cards > article").count();
  const goto = async (path) => {
    await page.goto("about:blank");
    await page.goto(base + path);
    await ready();
  };
  const status = (text) =>
    page.waitForFunction(
      (text) =>
        document
          .querySelector("[data-listing-status]")
          ?.textContent.includes(text),
      text,
    );
  const api = async (query) =>
    (await context.request.get(base + "/api/sources?" + query)).json();
  const inventory = await api("");
  const expectedSourceCount = Number(process.env.WEB_TEST_SOURCE_COUNT);
  assert(Number.isInteger(expectedSourceCount) && expectedSourceCount > 0);
  assert.equal(inventory.total, expectedSourceCount);
  const expectedNewspaperCount = Number(process.env.WEB_TEST_NEWSPAPER_COUNT);
  assert(
    Number.isInteger(expectedNewspaperCount) && expectedNewspaperCount >= 0,
  );
  assert.equal(
    inventory.kinds.find((k) => k.value === "newspaper")?.count ?? 0,
    expectedNewspaperCount,
  );
  const filtered = await api("kind=research_report");
  assert.equal(filtered.total, expected.reportTotal);
  const search = await api(new URLSearchParams({ q: expected.sourceQuery }));
  assert.equal(search.total, expected.searchTotal);
  await mkdir("test-results/web", { recursive: true });
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await goto("/sources");
    await page.locator("#q").fill(expected.sourceQuery);
    await page.getByRole("button", { name: "Apply filters" }).click();
    await page.waitForURL(
      (url) => url.searchParams.get("q") === expected.sourceQuery,
    );
    await ready();
    assert.equal(await count(), expected.searchTotal);
    await status(
      `Showing ${expected.searchTotal} of ${expected.searchTotal} sources.`,
    );
    assert(await page.locator("[data-listing-sentinel]").isHidden());
    assert.equal(await page.locator("[role=status]").count(), 1);
    assert.deepEqual((await accessibility(page).analyze()).violations, []);
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    await screenshot(page, {
      path: `test-results/web/source-filters-${family ? "family" : "public"}-${width}.png`,
      fullPage: true,
    });
    await page.locator("#kind").selectOption("record_image");
    await page.getByRole("button", { name: "Apply filters" }).click();
    await page.waitForURL(
      (url) => url.searchParams.get("kind") === "record_image",
    );
    await ready();
    assert.equal(await count(), expected.imageMatches);
    assert.equal(
      await page.locator("#kind option[value=research_report]").textContent(),
      `Research notes (${expected.reportMatches})`,
    );
    await page.locator("#q").fill("no-source-match-zzzz");
    await page.getByRole("button", { name: "Apply filters" }).click();
    await page.waitForURL(
      (url) => url.searchParams.get("q") === "no-source-match-zzzz",
    );
    await ready();
    assert.equal(await count(), 0);
    assert.match(
      await page.locator("[data-source-empty]").textContent(),
      /No sources match.*in Record images/,
    );
    assert.equal(await page.locator("#kind").inputValue(), "record_image");
    assert.equal(
      await page.locator("#kind option:checked").textContent(),
      "Record images (0)",
    );
    await page.locator("[data-clear-filters]").click();
    await page.waitForURL(base + "/sources");
    await ready();
    assert.equal(await page.locator("#q").inputValue(), "");
  }
  await page.setViewportSize({ width: 1280, height: 844 });
  await goto("/sources?kind=research_report");
  await manualListingOnly(page);
  const popupPromise = context.waitForEvent("page");
  await page
    .locator("[data-clear-filters]")
    .click({ modifiers: ["ControlOrMeta"] });
  const popup = await popupPromise;
  await popup.close();
  await page.locator("[data-load-more]").click();
  await status(
    `Showing ${expected.reportTotal} of ${expected.reportTotal} sources.`,
  );
  // A submit that does not navigate must also leave the current loader usable.
  await goto("/sources?kind=research_report");
  await manualListingOnly(page);
  await page.locator("form").evaluate((form) =>
    form.addEventListener("submit", (event) => event.preventDefault(), {
      once: true,
    }),
  );
  await page.getByRole("button", { name: "Apply filters" }).click();
  await page.locator("[data-load-more]").click();
  await status(
    `Showing ${expected.reportTotal} of ${expected.reportTotal} sources.`,
  );
  // Empty form q is equivalent to omitted q on subsequent URLs.
  await goto("/sources?q=&kind=research_report");
  await manualListingOnly(page);
  await page.locator("[data-load-more]").click();
  await status(
    `Showing ${expected.reportTotal} of ${expected.reportTotal} sources.`,
  );
  assert.equal(await count(), expected.reportTotal);
  const source = page.locator(".cards > article").nth(30).locator("h2 a");
  const sourceUrl = base + (await source.getAttribute("href"));
  await source.click();
  // Let the detail document finish loading before reversing navigation.
  await page.waitForURL(sourceUrl);
  await page.goBack();
  await page.waitForURL(base + "/sources?q=&kind=research_report");
  await ready();
  await status(
    `Showing ${expected.reportTotal} of ${expected.reportTotal} sources.`,
  );
  assert.equal(await page.locator("#kind").inputValue(), "research_report");
  assert.equal(new URL(page.url()).searchParams.get("kind"), "research_report");
  // A delayed old batch cannot append to a newly submitted filter.
  await goto("/sources?kind=research_report");
  await manualListingOnly(page);
  let release, started;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const began = new Promise((resolve) => {
    started = resolve;
  });
  const batch = "**/sources?*offset=25*";
  await page.route(batch, async (route) => {
    const response = await route.fetch();
    started();
    await gate;
    await route.fulfill({ response }).catch(() => {});
  });
  await page.locator("[data-load-more]").click();
  await Promise.race([
    began,
    new Promise((_, reject) =>
      setTimeout(() => reject(Error("Source batch did not start")), 15000),
    ),
  ]);
  await page.locator("#q").fill(expected.sourceQuery);
  await page.getByRole("button", { name: "Apply filters" }).click();
  await page.waitForURL(
    (url) => url.searchParams.get("q") === expected.sourceQuery,
  );
  release();
  await ready();
  await page.unroute(batch);
  assert.equal(await count(), expected.reportMatches);
  await status(
    `Showing ${expected.reportMatches} of ${expected.reportMatches} sources.`,
  );
  // Publication reset preserves both filters and refreshes count/control metadata.
  // Start from page one so a second page exists; keep explicit offset in the URL.
  await goto("/sources?q=&kind=research_report&offset=0");
  await manualListingOnly(page);
  await page.locator("#q").fill("Unsubmitted title");
  await page.locator("#kind").selectOption("newspaper");
  await page.route("**/sources?*offset=*", async (route) => {
    const url = new URL(route.request().url());
    assert.equal(url.searchParams.get("kind"), "research_report");
    const response = await route.fetch();
    const body = (await response.text()).replace(
      /data-listing-token="[^"]+"/,
      `data-listing-token="${family ? "family" : "public"}:${"d".repeat(64)}"`,
    );
    await route.fulfill({ response, body });
  });
  await page.locator("[data-load-more]").click();
  await status("archive changed");
  assert.equal(await page.locator("#q").inputValue(), "Unsubmitted title");
  assert.equal(await page.locator("#kind").inputValue(), "newspaper");
  assert.equal(await count(), 25);
  assert.equal(new URL(page.url()).searchParams.get("kind"), "research_report");
  assert.equal(new URL(page.url()).searchParams.has("offset"), false);
  await page.unroute("**/sources?*offset=*");
  // No-JS readers retain the same filter and bounded pagination contract.
  const plain = await context.newPage();
  const noJS = await context.newCDPSession(plain);
  await noJS.send("Emulation.setScriptExecutionDisabled", { value: true });
  try {
    await plain.goto(base + "/sources?q=&kind=research_report");
    await plain.getByRole("link", { name: "Next page" }).click();
    await plain.waitForLoadState("domcontentloaded");
    assert.equal(
      new URL(plain.url()).searchParams.get("kind"),
      "research_report",
    );
    assert.equal(
      await plain.locator(".cards > article").count(),
      expected.reportTotal - 25,
    );
    assert.equal(
      await plain.locator("[data-listing-status]").innerText(),
      `Showing 26–${expected.reportTotal} of ${expected.reportTotal} sources.`,
    );
    await plain.getByRole("link", { name: "Previous page" }).click();
    await plain.waitForLoadState("domcontentloaded");
    assert.equal(await plain.locator(".cards > article").count(), 25);
  } finally {
    await plain.close();
  }
  await goto("/sources?kind=research_report&offset=25");
  await status(
    `Showing 26–${expected.reportTotal} of ${expected.reportTotal} sources.`,
  );
  console.log(
    "PASS: Sources filters, counts, responsive/axe, Back, late response, publication restart and no-JS",
  );
}
