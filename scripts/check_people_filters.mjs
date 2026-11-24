import { screenshot } from "./browser_checks.mjs";
import { manualListingOnly } from "./check_navigation.mjs";
import assert from "node:assert/strict";

export async function checkPeopleFilters(
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
  const goto = async (path) => {
    await page.goto("about:blank");
    await page.goto(base + path);
    await ready();
  };
  const count = (n) =>
    page.waitForFunction(
      (n) => document.querySelectorAll(".cards > article").length === n,
      n,
    );
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await goto("/people?" + new URLSearchParams({ q: expected.treeQuery }));
    assert.equal(await page.locator("#scope").inputValue(), "tree");
    assert.equal(
      await page
        .locator(`.cards a[href="/people/${expected.treePerson}"]`)
        .count(),
      1,
    );
    await page.locator("#q").fill(expected.otherQuery);
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.waitForURL(
      (url) => url.searchParams.get("q") === expected.otherQuery,
    );
    await ready();
    await count(0);
    await page.locator("#scope").selectOption("all");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await page.waitForURL((url) => url.searchParams.get("scope") === "all");
    await ready();
    assert.equal(
      await page
        .locator(`.cards a[href="/people/${expected.otherPerson}"]`)
        .count(),
      1,
    );
    await screenshot(page, {
      path: `test-results/web/people-filters-${family ? "family" : "public"}-${width}.png`,
      fullPage: true,
    });
    await page.locator("[data-clear-filters]").click();
    await page.waitForURL(base + "/people");
    await ready();
    assert.equal(await page.locator("#scope").inputValue(), "tree");
    assert.equal(await page.locator("#q").inputValue(), "");
  }
  await page.setViewportSize({ width: 1280, height: 844 });
  await goto("/people?scope=all");
  await manualListingOnly(page);
  await page.locator("[data-load-more]").click();
  await count(50);
  const profile = page.locator(".cards > article").nth(30).locator("h3 a");
  const profileUrl = base + (await profile.getAttribute("href"));
  await profile.click();
  // click() waits for commit, not for the destination's load lifecycle.
  await page.waitForURL(profileUrl);
  await page.goBack();
  await page.waitForURL(base + "/people?scope=all");
  await ready();
  await count(50);
  assert.equal(await page.locator("#scope").inputValue(), "all");

  // An old All-people batch must not append after changing to Family tree.
  await goto("/people?scope=all");
  await manualListingOnly(page);
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  await page.route("**/people?*offset=25*", async (route) => {
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response }).catch(() => {});
  });
  const request = page.waitForRequest(
    (r) => r.resourceType() === "fetch" && r.url().includes("offset=25"),
  );
  await page.locator("[data-load-more]").click();
  await request;
  await page.locator("#scope").selectOption("tree");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.waitForURL((url) => url.searchParams.get("scope") === "tree");
  release();
  await ready();
  await page.unroute("**/people?*offset=25*");
  await count(25);
  const expectedIds = (
    await (await context.request.get(base + "/api/people")).json()
  ).people.map((p) => p.id);
  const actual = await page
    .locator(".cards h3 a")
    .evaluateAll((links) => links.map((a) => a.pathname.split("/").at(-1)));
  assert.deepEqual(actual, expectedIds);

  await goto("/people?scope=all&offset=25");
  await manualListingOnly(page);
  await page.route("**/people?*offset=*", async (route) => {
    assert.equal(
      new URL(route.request().url()).searchParams.get("scope"),
      "all",
    );
    const response = await route.fetch();
    await route.fulfill({
      response,
      body: (await response.text()).replace(
        /data-listing-token="[^"]+"/,
        `data-listing-token="${family ? "family" : "public"}:${"e".repeat(64)}"`,
      ),
    });
  });
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(() =>
    document
      .querySelector("[data-listing-status]")
      .textContent.includes("archive changed"),
  );
  assert.equal(new URL(page.url()).searchParams.get("scope"), "all");
  assert.equal(new URL(page.url()).searchParams.has("offset"), false);
  await count(25);
  await page.unroute("**/people?*offset=*");

  const plain = await context.newPage();
  const session = await context.newCDPSession(plain);
  await session.send("Emulation.setScriptExecutionDisabled", { value: true });
  try {
    await plain.goto(base + "/people?scope=all&q=");
    await plain.getByRole("link", { name: "Next page" }).click();
    await plain.waitForLoadState("domcontentloaded");
    assert.equal(new URL(plain.url()).searchParams.get("scope"), "all");
    assert.equal(await plain.locator(".cards > article").count(), 25);
    await plain.getByRole("link", { name: "Previous page" }).click();
    await plain.waitForLoadState("domcontentloaded");
    assert.equal(await plain.locator("#scope").inputValue(), "all");
  } finally {
    await plain.close();
  }
  console.log(
    "PASS: People default Family tree, All people, owner examples, Back, late batches, publication restart and no-JS",
  );
}
