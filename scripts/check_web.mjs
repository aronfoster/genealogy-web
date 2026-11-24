import { checkNavigation } from "./check_navigation.mjs";
import { checkListings } from "./check_listing.mjs";
import { checkPeopleFilters } from "./check_people_filters.mjs";
import { checkSourceFilters } from "./check_source_filters.mjs";
import assert from "node:assert/strict";
import { browserInputs } from "./browser_inputs.mjs";
import { mkdir } from "node:fs/promises";
import { chromium } from "playwright";
import packagedChromium from "@sparticuz/chromium";
import {
  accessibility,
  screenshot,
  failureScreenshots,
} from "./browser_checks.mjs";
const base = process.env.WEB_TEST_BASE_URL;
assert(base, "Run through scripts/check_web.py with TEST_DATABASE_URL");
const namesPreview = process.env.WEB_TEST_NAMES_PREVIEW === "1";
const sourcePreview = process.env.WEB_TEST_SOURCE_PREVIEW === "1";
const historical = process.env.WEB_TEST_HISTORICAL === "1";
const familyAudience = process.env.WEB_TEST_FAMILY === "1";
const people = process.env.WEB_TEST_PEOPLE.split(",");
const screenshots =
  process.env.WEB_TEST_ARTIFACT_DIR ||
  `test-results/web/${namesPreview ? "names-preview" : sourcePreview ? "source-preview" : historical ? "historical" : "current"}${familyAudience ? "-family" : ""}`;
await mkdir(screenshots, { recursive: true });
const expected = browserInputs();
const ids = historical
  ? expected
  : {
      senior: people[0],
      child: people[1] || people[0],
      infant: people[2] || people[0],
    };
const relationshipCases = historical ? [] : expected.relationshipPeople;
const peopleSearch = (term) =>
  "/people?" + new URLSearchParams({ scope: "all", q: term });
const historicalQuery = historical
  ? new URLSearchParams({ q: expected.query }).toString()
  : null;
const browser = await chromium.launch({
  headless: true,
  ...(process.platform === "linux" && process.arch === "x64"
    ? {
        executablePath: await packagedChromium.executablePath(),
        // Preserve Playwright's navigation compatibility flags (RenderDocument,
        // PaintHolding, etc.); a second --disable-features replaces its list.
        args: packagedChromium.args.filter(
          (arg) => !arg.startsWith("--disable-features="),
        ),
      }
    : {}),
});
try {
  const context = await browser.newContext();
  await context.route("**/*", (route) =>
    new URL(route.request().url()).origin === base
      ? route.continue()
      : route.abort(),
  );
  const page = await context.newPage();
  async function waitForReader() {
    await page.waitForLoadState();
    if (familyAudience)
      await page.waitForFunction(
        () =>
          !document.documentElement.classList.contains("family-revalidating"),
      );
  }
  async function visit(path) {
    const response = await page.goto(base + path);
    await waitForReader();
    return response;
  }
  if (!historical) {
    const aliasPerson = expected.aliasPerson;
    for (const q of expected.aliases) {
      const result = await (
        await context.request.get(
          base + "/api/people?q=" + encodeURIComponent(q),
        )
      ).json();
      assert.deepEqual(
        result.people.map((p) => p.id),
        [aliasPerson],
      );
    }
    if (namesPreview) {
      const result = await (
        await context.request.get(
          base +
            "/api/people?" +
            new URLSearchParams({ q: expected.previewQuery }),
        )
      ).json();
      assert.deepEqual(
        result.people.map((p) => p.id),
        [expected.previewPerson],
      );
      const profile = await (
        await context.request.get(base + "/api/people/" + aliasPerson)
      ).json();
      assert.equal(profile.name, expected.preferredName);
      assert(
        profile.name_details.some(
          (n) =>
            n.label === "Birth / maiden name" && n.name === expected.birthName,
        ),
      );
      assert.deepEqual(profile.compact_dates, [expected.compactDate]);
    }
  }
  await checkNavigation(context, page, base, familyAudience, screenshots);
  if (!historical && !sourcePreview && !namesPreview) {
    await checkSourceFilters(context, page, base, familyAudience, expected);
    await checkPeopleFilters(context, page, base, familyAudience, expected);
    await checkListings(context, page, base, familyAudience);
  }
  let neutralProfile = null;
  for (let offset = 0; neutralProfile === null; ) {
    const response = await context.request.get(
      base + `/api/people?scope=all&offset=${offset}`,
    );
    assert.equal(response.status(), 200);
    const inventory = await response.json();
    for (const person of inventory.people) {
      const family = await (
        await context.request.get(base + "/api/family/" + person.id)
      ).json();
      if (Object.keys(family.groups).length === 0) {
        neutralProfile = person.id;
        break;
      }
    }
    if (neutralProfile !== null || inventory.next === null) break;
    assert.equal(typeof inventory.next, "number");
    offset = inventory.next;
  }
  assert(
    neutralProfile,
    "Browser fixture must include a published person with no family groups",
  );
  await mkdir(screenshots, { recursive: true });
  // This harness imports one immutable dataset and fixes its audience for the
  // process. Expected API models do not depend on viewport. Read each once,
  // while still navigating/rendering and checking every page at every width.
  // Do not share this cache with listing races, auth tests, or another variant.
  const expectedModels = new Map();
  async function expectedModel(path) {
    if (!expectedModels.has(path)) {
      const response = await context.request.get(base + path);
      assert.equal(response.status(), 200, path);
      expectedModels.set(path, await response.json());
    }
    return expectedModels.get(path);
  }
  let checkedLayouts = 0;
  for (const viewport of [
    { width: 1280, height: 900 },
    { width: 390, height: 844 },
    { width: 320, height: 800 },
  ]) {
    await page.setViewportSize(viewport);
    const routes = [
      "/",
      historical ? "/people?" + historicalQuery : "/people",
      "/people/" + ids.senior,
      "/people/" + ids.child,
      "/people/" + ids.infant,
      "/family/" + ids.child,
      "/sources",
      "/sources",
      "/people/missing",
      "/access",
      ...relationshipCases.map((id) => "/people/" + id),
      ...(historical ? [] : expected.searchTerms.map(peopleSearch)),
    ];
    // Select a real visible source rather than a hard-coded source key.
    const source = await expectedModel("/api/sources");
    if (source.sources.length) routes[7] = "/sources/" + source.sources[0].id;
    for (const [index, route] of routes.entries()) {
      checkedLayouts += 1;
      const response = await visit(route);
      assert.equal(
        response.status(),
        route === "/people/missing" ? 404 : 200,
        route,
      );
      if (route.startsWith("/people/per_")) {
        const profile = await expectedModel("/api" + route);
        const model = profile.relationships;
        for (const group of ["Parents", "Spouses", "Siblings", "Children"]) {
          assert.equal(
            await page
              .getByRole("heading", { name: group, exact: true })
              .count(),
            model.groups[group] ? 1 : 0,
          );
          for (const relative of model.groups[group] || []) {
            const section = page
              .locator("section")
              .filter({
                has: page.locator(`#relationship-${group.toLowerCase()}`),
              })
              .last();
            assert(
              (await section
                .locator(`a[href="/people/${relative.id}"]`)
                .count()) > 0,
            );
            assert((await section.innerText()).includes(relative.relationship));
            for (const date of relative.dates)
              assert((await section.innerText()).includes(date.text));
          }
        }
        assert.equal(
          await page
            .getByRole("heading", {
              name: "Uncertain relationships",
              exact: true,
            })
            .count(),
          model.uncertain.length ? 1 : 0,
        );
        const evidence = page.locator(".relationship-row details").first();
        if (await evidence.count()) {
          await evidence.locator("summary").focus();
          await page.keyboard.press("Enter");
          assert.equal(await evidence.getAttribute("open"), "");
          assert.match(
            await evidence.locator("a").first().getAttribute("aria-label"),
            /^Evidence: .+ — .+/,
          );
          await page.keyboard.press("Enter");
        }
      }
      if (
        !historical &&
        expected.sparseTerms.map(peopleSearch).includes(route)
      ) {
        const inventory = await expectedModel("/api" + route);
        const sparse = inventory.people.find((p) =>
          expected.sparsePeople.includes(p.id),
        );
        assert(sparse, "Expected the sparse estate record in search results");
        const contextLink = sparse.context.find((c) =>
          c.text.includes(expected.estateContext),
        );
        assert(
          contextLink,
          "Sparse cards must link to their published record context",
        );
        assert(
          (await page.locator(`a[href="${contextLink.url}"]`).count()) > 0,
        );
      }
      await page.evaluate(() => document.fonts.ready);
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
        `overflow: ${route} at ${viewport.width}`,
      );
      const audit = await accessibility(page)
        .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
        .analyze();
      assert.deepEqual(
        audit.violations.map((v) => ({
          id: v.id,
          nodes: v.nodes.map((n) => n.target),
        })),
        [],
        route,
      );
      if (viewport.width !== 320)
        await screenshot(page, {
          path: `${screenshots}/${viewport.width}-${index}.png`,
          fullPage: true,
        });
    }
    await visit("/people/" + neutralProfile);
    assert.equal(
      await page.getByRole("link", { name: "Explore nearby family" }).count(),
      0,
    );
    assert.equal(
      await page
        .getByRole("link", { name: "Browse people in the archive" })
        .count(),
      1,
    );
    if (historical) {
      await visit("/");
      await page
        .getByLabel("Search names, dates or places")
        .fill(expected.query);
      await page.getByRole("button", { name: "Search", exact: true }).click();
      await page.waitForURL("**/people?" + historicalQuery + "&scope=tree");
      await page.locator(".person-card").first().waitFor();
      assert.equal(
        await page.locator(".person-card").count(),
        expected.matches,
      );
      await visit("/people/" + ids.child);
      await page.getByRole("link", { name: "Explore nearby family" }).click();
      await waitForReader();
      assert.match(await page.locator("h1").innerText(), /Family of/);
      await page.locator(`a[href="/people/${ids.senior}"]`).first().click();
      await waitForReader();
      assert((await page.locator("h1").innerText()).includes(expected.heading));
      await page.goBack();
      await waitForReader();
      assert.match(page.url(), new RegExp(ids.child));
    } else {
      const inventory = await expectedModel("/api/people");
      const named = inventory.people.find(
        (person) => person.name !== "Name not yet established",
      );
      if (named) {
        await visit("/");
        await page.getByLabel("Search names, dates or places").fill(named.name);
        await page.getByRole("button", { name: "Search", exact: true }).click();
        await page.waitForURL(
          (url) =>
            url.pathname === "/people" &&
            url.searchParams.get("q") === named.name,
        );
        await page.locator(".person-card").first().waitFor();
        assert((await page.locator(".person-card").count()) > 0);
      }
      await visit("/people/" + ids.child);
      await page.getByRole("link", { name: "Explore nearby family" }).click();
      await waitForReader();
      assert.match(await page.locator("h1").innerText(), /Family of/);
      await page.goBack();
      await waitForReader();
      assert.match(page.url(), new RegExp(ids.child));
    }
  }
  if (sourcePreview) {
    const sourceId = expected.previewSource;
    for (const width of [1280, 390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      assert.equal((await visit("/sources/" + sourceId)).status(), 200);
      const links = page.locator(".evidence-locations a");
      assert.equal(await links.count(), expected.previewLinks);
      const names = await links.allTextContents();
      assert.equal(new Set(names).size, expected.previewLinks);
      for (const name of names) assert(name.includes("external site"));
      for (const link of await links.all()) {
        assert.equal(await link.getAttribute("target"), "_blank");
        const rel = (await link.getAttribute("rel")) || "";
        assert(rel.includes("noopener") && rel.includes("noreferrer"));
        assert(
          (await link.textContent()).includes(
            "external site, opens in new tab",
          ),
        );
      }
      assert.equal(
        await page.locator('a[href="/sources/' + sourceId + '"]').count(),
        0,
      );
      assert.equal(await page.locator('a[href^="/assets/"]').count(), 0);
      assert.equal(
        await page
          .getByRole("heading", { level: 3, name: "Citation 1" })
          .count(),
        1,
      );
      const disclosure = page.locator(".original-citation");
      assert.equal(await disclosure.getAttribute("open"), null);
      await disclosure.locator("summary").focus();
      await page.keyboard.press("Enter");
      assert.notEqual(await disclosure.getAttribute("open"), null);
      assert((await disclosure.innerText()).includes(expected.citationText));
      await page.keyboard.press("Enter");
      await links.first().focus();
      assert.equal(await page.locator(":focus").textContent(), names[0]);
      await page.keyboard.press("Tab");
      assert.equal(await page.locator(":focus").textContent(), names[1]);
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
      );
      const audit = await accessibility(page)
        .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
        .analyze();
      assert.deepEqual(audit.violations, []);
      await screenshot(page, {
        path: `${screenshots}/${width}-curated-source.png`,
        fullPage: true,
      });
    }
    const source = await (
      await context.request.get(base + "/api/sources/" + sourceId)
    ).json();
    assert(source.used_for.length > 0);
    const claimId = source.used_for[0].id;
    assert.equal((await visit("/assertions/" + claimId)).status(), 200);
    assert(
      (await page.locator(".evidence-locations a").count()) >=
        expected.previewLinks,
    );
    assert.deepEqual((await accessibility(page).analyze()).violations, []);
  }
  await visit("/");
  await page.keyboard.press("Tab");
  assert.equal(await page.locator(":focus").innerText(), "Skip to content");
  await page.keyboard.press("Enter");
  assert.equal(await page.locator(":focus").getAttribute("id"), "main");
  console.log(
    `Application browser checks passed: ${checkedLayouts} layouts, axe, relationship profiles, search, family navigation, keyboard and history.`,
  );
} catch (error) {
  await failureScreenshots(browser, screenshots);
  throw error;
} finally {
  await browser.close();
}
