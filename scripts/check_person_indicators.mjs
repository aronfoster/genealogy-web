import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { chromium } from "playwright";
import packagedChromium from "@sparticuz/chromium";
import {
  accessibility,
  screenshot,
  failureScreenshots,
} from "./browser_checks.mjs";

import { browserInputs } from "./browser_inputs.mjs";
const inputs = browserInputs();
const base = process.env.INDICATOR_TEST_URL;
assert(base, "Run through scripts/check_person_indicators.py");
const browser = await chromium.launch({
  executablePath: await packagedChromium.executablePath(),
  args: packagedChromium.args.filter(
    (arg) => !arg.startsWith("--disable-features="),
  ),
  headless: true,
});

async function checkHeadingOrder(container, headingSelector) {
  assert(
    await container.evaluate((el, selector) => {
      const heading = el.querySelector(selector);
      const indicators = el.querySelectorAll(
        ".person-membership, .person-attributes",
      );
      return [...indicators].every(
        (indicator) =>
          Boolean(
            heading.compareDocumentPosition(indicator) &
            Node.DOCUMENT_POSITION_FOLLOWING,
          ) &&
          indicator.getBoundingClientRect().top >=
            heading.getBoundingClientRect().bottom,
      );
    }, headingSelector),
    "Indicators follow the heading in reading order and follow it visually",
  );
  const accessible = await container.ariaSnapshot();
  const headingPosition = accessible.indexOf("heading ");
  assert(headingPosition >= 0);
  for (const text of await container
    .locator(".person-indicators summary[aria-label]")
    .evaluateAll((items) =>
      items.map((el) => el.getAttribute("aria-label") || el.textContent),
    )) {
    assert(
      accessible.indexOf(text.trim(), headingPosition) > headingPosition,
      `Heading navigation can read forward to ${text.trim()}`,
    );
  }
}

try {
  const context = await browser.newContext();
  const page = await context.newPage();
  await mkdir("test-results/person-indicators", { recursive: true });
  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto(base + "/indicator-review");
    await page.locator(".person-card").first().waitFor();
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.locator(".person-card").count(), 13);
    const headerHeights = await page
      .locator(".person-membership")
      .evaluateAll((items) =>
        items.map((item) => item.getBoundingClientRect().height),
      );
    assert(Math.max(...headerHeights) <= 42, "Membership remains compact");
    assert.equal(await page.locator(".membership-other").count(), 0);
    assert.equal(await page.locator(".membership-possible circle").count(), 2);
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    for (const card of await page.locator(".person-card").all()) {
      await checkHeadingOrder(card, "h3");
      assert.equal(
        await card.evaluate((el) => getComputedStyle(el).borderStyle),
        "solid",
      );
    }
    const overlap = page.locator(".person-card").nth(5);
    const accessible = await overlap.ariaSnapshot();
    for (const text of ["Deceased", "Died as a child", "Slave"]) {
      assert(accessible.includes(text), text);
    }
    assert(
      !accessible.includes('img "'),
      "Visible labels replace redundant SVG announcements",
    );
    assert.equal(
      await page
        .locator(".person-card")
        .nth(8)
        .locator(".person-attributes")
        .count(),
      0,
    );
    const summaries = page.locator(".person-card summary");
    const first = summaries.first();
    await first.hover();
    assert(await first.locator("..").locator(".indicator-tooltip").isVisible());
    await page.keyboard.press("Escape");
    assert.equal(
      await first.locator("..").locator(".indicator-tooltip").isVisible(),
      false,
      "Escape dismisses hover disclosure without requiring focus",
    );
    await page.locator("h1").hover();
    await first.hover();
    await first.focus();
    await page.keyboard.press("Enter");
    assert((await first.locator("..").getAttribute("open")) !== null);
    assert(await first.locator("..").locator(".indicator-tooltip").isVisible());
    await page.keyboard.press("Escape");
    assert.equal(await first.locator("..").getAttribute("open"), null);
    assert.equal(
      await first.locator("..").locator(".indicator-tooltip").isVisible(),
      false,
    );
    await first.click();
    assert(await first.locator("..").locator(".indicator-tooltip").isVisible());
    await page.locator("h1").click();
    assert.equal(await first.locator("..").getAttribute("open"), null);
    assert((await first.ariaSnapshot()).includes("In the family tree"));
    const ids = await page
      .locator("[id]")
      .evaluateAll((items) => items.map((i) => i.id));
    assert.equal(new Set(ids).size, ids.length, "Unique description IDs");
    const axe = await accessibility(page)
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    assert.deepEqual(axe.violations, []);
    await screenshot(page, {
      path: `test-results/person-indicators/cards-${width}.png`,
      fullPage: true,
    });
    await writeFile(
      `test-results/person-indicators/accessible-${width}.txt`,
      await page.locator("main").ariaSnapshot(),
    );
    await page.goto(base + "/indicator-profile");
    await page.evaluate(() => document.fonts.ready);
    await checkHeadingOrder(page.locator(".person-heading"), "h1");
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    const profileAxe = await accessibility(page)
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    assert.deepEqual(profileAxe.violations, []);
    await screenshot(page, {
      path: `test-results/person-indicators/profile-${width}.png`,
      fullPage: true,
    });
  }
  const touchContext = await browser.newContext({
    viewport: { width: 320, height: 900 },
    hasTouch: true,
  });
  const touch = await touchContext.newPage();
  await touch.goto(base + "/indicator-review");
  const touchSummary = touch.locator(".person-card summary").first();
  await touchSummary.tap();
  assert(
    await touchSummary.locator("..").locator(".indicator-tooltip").isVisible(),
  );
  await touchSummary.tap();
  assert.equal(
    await touchSummary.locator("..").locator(".indicator-tooltip").isVisible(),
    false,
  );
  // Keep the context until browser shutdown: the packaged single-process
  // Chromium cannot close one context while another remains active.
  if (process.env.INDICATOR_ACTUAL) {
    const actual = inputs.actual;
    assert.deepEqual(
      actual
        .filter((entry) => entry.reviewed)
        .map((entry) => entry.id)
        .sort(),
      inputs.reviewed,
      "Every currently permitted reviewed identity must be rendered",
    );
    for (const width of [1280, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(base + "/indicator-actual");
      await page.evaluate(() => document.fonts.ready);
      for (const entry of actual.filter((entry) => entry.reviewed)) {
        const card = page.locator(".person-card").filter({
          has: page.locator(`h3 a[href="/people/${entry.id}"]`),
        });
        assert.equal(await card.count(), 1, "Reviewed real person is present");
        assert.deepEqual(
          await card
            .locator(".person-attributes summary")
            .evaluateAll((items) =>
              items.map((item) => item.getAttribute("aria-label")),
            ),
          entry.attributes.map((item) => item.label),
          "Each reviewed real person keeps the expected indicators",
        );
      }
      assert.equal(await page.locator(".membership-other").count(), 0);
      for (const card of await page.locator(".person-card").all())
        await checkHeadingOrder(card, "h3");
      assert.deepEqual(
        (
          await accessibility(page)
            .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
            .analyze()
        ).violations,
        [],
      );
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      );
      await screenshot(page, {
        path: `test-results/person-indicators/actual-${width}.png`,
        fullPage: true,
      });
    }
  }
  await page.goto(base + "/indicator-review");
  await page.emulateMedia({ forcedColors: "active" });
  assert.equal(
    await page
      .locator(".membership-possible")
      .evaluate((el) => getComputedStyle(el).borderStyle),
    "solid",
  );
  await screenshot(page, {
    path: "test-results/person-indicators/forced-colors-320.png",
    fullPage: true,
  });
  console.log(
    "Person indicators: card/profile reading and visual order, desktop/mobile/320px, axe, accessible names and forced colors passed",
  );
} catch (error) {
  await failureScreenshots(browser, "test-results/person-indicators");
  throw error;
} finally {
  await browser.close();
}
