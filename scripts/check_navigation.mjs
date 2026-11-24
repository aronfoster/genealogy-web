import assert from "node:assert/strict";
import { accessibility, screenshot } from "./browser_checks.mjs";

// Isolate manual-request races from IntersectionObserver. Automatic loading is
// exercised separately with the real, visible sentinel in check_listing.mjs.
export async function manualListingOnly(page) {
  await page.locator("[data-listing-sentinel]").evaluate((node) => {
    node.style.display = "none";
  });
}

export async function checkNavigation(
  context,
  page,
  base,
  family,
  directory = "test-results/web",
) {
  const ready = async () => {
    await page.waitForFunction(
      () => !document.documentElement.classList.contains("family-revalidating"),
    );
  };
  for (const [width, height] of [
    [1280, 844],
    [390, 844],
    [320, 844],
    [390, 360],
  ]) {
    await page.setViewportSize({ width, height });
    await page.goto(base + "/people?scope=all");
    await ready();
    assert.equal(await page.locator("[data-listing-footer]").count(), 0);
    assert.equal(
      await page
        .locator(".cards .eyebrow")
        .filter({ hasText: /^Person$/ })
        .count(),
      0,
    );
    await page.evaluate(() =>
      window.scrollTo(0, document.body.scrollHeight / 2),
    );
    assert(await page.evaluate(() => window.scrollY > 100));
    const menu = page.locator("[data-site-menu]");
    const toggle = menu.locator("summary");
    const box = await toggle.boundingBox();
    assert(box.y >= 0 && box.y + box.height <= height);
    await toggle.focus();
    await page.keyboard.press("Enter");
    assert(await menu.evaluate((node) => node.open));
    const nav = page.getByRole("navigation", { name: "Main", exact: true });
    for (const [label, href] of [
      ["Home", "/"],
      ["People", "/people"],
      ["Sources", "/sources"],
      ["About this archive", "/about"],
      [family ? "Family access active" : "Family access", "/access"],
    ]) {
      assert.equal(
        await nav
          .getByRole("link", { name: label, exact: true })
          .getAttribute("href"),
        href,
      );
    }
    assert.equal(
      await nav
        .getByRole("link", { name: "Account and sign out", exact: true })
        .count(),
      0,
    );
    assert.equal(
      await nav.getByRole("link", { name: "My profile", exact: true }).count(),
      family ? 1 : 0,
    );
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    const menuBox = await nav.boundingBox();
    assert(menuBox.x >= 0 && menuBox.x + menuBox.width <= width);
    assert(menuBox.y + menuBox.height <= height);
    const results = await accessibility(page)
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    assert.deepEqual(
      results.violations,
      [],
      `navigation axe ${width}x${height}`,
    );
    await screenshot(page, {
      path: `${directory}/navigation-${family ? "family" : "public"}-${width}x${height}.png`,
    });
    await page.keyboard.press("Escape");
    assert.equal(await menu.evaluate((node) => node.open), false);
    assert(await toggle.evaluate((node) => document.activeElement === node));
    await page.keyboard.press("Space");
    await page.keyboard.press("Tab");
    assert(
      await nav
        .getByRole("link", { name: "Home", exact: true })
        .evaluate((node) => document.activeElement === node),
    );
    // Tabbing out dismisses the panel before it can cover focused page content.
    await page.locator("#q").focus();
    assert.equal(await menu.evaluate((node) => node.open), false);
    await toggle.click();
    await page.locator(".site-header").click({ position: { x: 3, y: 3 } });
    assert.equal(await menu.evaluate((node) => node.open), false);
    await toggle.click();
    await nav
      .getByRole("link", { name: "About this archive", exact: true })
      .click();
    await page.waitForURL(base + "/about");
    await ready();
    assert(
      await page
        .getByRole("heading", { name: "About this archive", exact: true })
        .isVisible(),
    );
    assert.match(
      await page.locator("main").textContent(),
      /Conclusions can change as evidence grows/,
    );
  }
  const fallback = await context.newPage();
  const nojs = await context.newCDPSession(fallback);
  await nojs.send("Emulation.setScriptExecutionDisabled", { value: true });
  await fallback.setViewportSize({ width: 390, height: 844 });
  await fallback.goto(base + "/people?scope=all");
  await fallback.evaluate(() => window.scrollTo(0, 700));
  const toggle = fallback.locator("[data-site-menu] summary");
  await toggle.focus();
  await fallback.keyboard.press("Enter");
  await fallback
    .getByRole("navigation", { name: "Main", exact: true })
    .getByRole("link", { name: "Sources", exact: true })
    .click();
  await fallback.waitForURL(base + "/sources");
  assert(await fallback.locator("h1").isVisible());
  await fallback.close();
  console.log(
    "PASS: compact navigation, deep scrolling, keyboard dismissal, About, card headings and no-JS navigation",
  );
}

// Production templates with the real session flow, called by the HTTPS harness.
export async function checkAccessAppearance(page, base, state) {
  const labels = {
    anonymous: "Sign in for family access",
    pending: "Signed in · access pending",
    approved: "Family access active",
    revoked: "Family access inactive",
  };
  const approved = state === "approved";
  const boxes = [];
  for (const width of [1280, 390, 360, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto(`${base}/about`);
    await page.waitForFunction(
      () => !document.documentElement.classList.contains("family-revalidating"),
    );
    await page.evaluate(() => document.fonts.ready);
    const menu = page.locator("[data-site-menu]");
    const toggle = menu.locator("summary");
    const icon = toggle.locator(".family-access-icon");
    assert.equal(await toggle.getAttribute("title"), null);
    assert.equal(await icon.count(), approved ? 1 : 0);
    assert.equal(
      await page.locator("body[data-family-document]").count(),
      approved ? 1 : 0,
    );
    assert.equal(
      await page.locator('script[src*="family-session.js"]').count(),
      approved ? 1 : 0,
    );
    assert.match(
      await toggle.ariaSnapshot(),
      approved ? /Menu\s*, family access active/ : /Menu/,
    );
    const box = await toggle.boundingBox();
    assert(box.height >= 44 && box.width >= 44);
    boxes.push([width, box.x, box.y, box.width, box.height]);
    if (approved) {
      assert(await icon.isVisible());
      await page.emulateMedia({ forcedColors: "active" });
      assert(await icon.isVisible());
      assert.notEqual(
        await icon.evaluate((el) => getComputedStyle(el).stroke),
        "none",
      );
      await screenshot(page, {
        path: `test-results/auth/access-approved-${width}-forced-colors.png`,
      });
      await page.emulateMedia({ forcedColors: "none" });
      const tooltip = toggle.locator(".access-tooltip");
      await toggle.hover();
      await tooltip.waitFor({ state: "visible" });
      await tooltip.hover(); // Explanation remains hoverable.
      assert(await tooltip.isVisible());
      await page.keyboard.press("Escape");
      await tooltip.waitFor({ state: "hidden" });
      assert.equal(await menu.evaluate((el) => el.open), false);
      await page.mouse.move(0, 800);
      await toggle.focus();
      await tooltip.waitFor({ state: "visible" });
      await page.keyboard.press("Escape");
      await tooltip.waitFor({ state: "hidden" });
    }
    await page.mouse.move(0, 800);
    await screenshot(page, {
      path: `test-results/auth/access-${state}-${width}-closed.png`,
    });
    await toggle.focus();
    await page.keyboard.press("Enter");
    assert(await menu.evaluate((el) => el.open));
    const access = menu.locator("[data-access-link]");
    assert.equal((await access.textContent()).trim(), labels[state]);
    assert.equal(await access.getAttribute("href"), "/access");
    assert(await access.isVisible());
    assert.equal(
      await menu
        .getByRole("link", { name: "Account and sign out", exact: true })
        .count(),
      0,
    );
    assert.equal(
      await menu.getByRole("link", { name: "My profile", exact: true }).count(),
      approved ? 1 : 0,
    );
    const nav = await menu.locator("nav").boundingBox();
    assert(nav.x >= 0 && nav.x + nav.width <= width);
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    const a11y = await accessibility(page)
      .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
      .analyze();
    assert.deepEqual(a11y.violations, [], `${state} ${width}`);
    await screenshot(page, {
      path: `test-results/auth/access-${state}-${width}-open.png`,
    });
    await page.keyboard.press("Escape");
    assert.equal(await menu.evaluate((el) => el.open), false);
    await page.goto(`${base}/access`);
    assert.equal(await page.locator("body[data-family-document]").count(), 0);
    assert.equal(
      await page.locator('script[src*="family-session.js"]').count(),
      approved ? 1 : 0,
    );
    await page.waitForFunction(
      () => !document.documentElement.classList.contains("family-revalidating"),
    );
    assert.equal(
      await page.locator("[data-access-link]").getAttribute("aria-current"),
      "page",
    );
    await screenshot(page, {
      path: `test-results/auth/access-page-${state}-${width}.png`,
      fullPage: true,
    });
  }
  return boxes;
}

export async function checkAccessRevalidation(page, base) {
  const statusURL = `${base}/api/access/status`;
  for (const path of ["/access?next=%2Fpeople", "/access/complete"]) {
    for (const failure of ["http", "network", "invalid", "timeout"]) {
      let release;
      const intercepted = new Promise((resolve) => {
        release = resolve;
      });
      await page.route(statusURL, (route) => release(route));
      await page.goto(base + path);
      const route = await intercepted;
      const logout = page.getByRole("button", {
        name: "Sign out",
        exact: true,
      });
      await page.waitForFunction(() =>
        document.documentElement.classList.contains("family-revalidating"),
      );
      // A pending request must not conceal or disable account controls.
      assert(await logout.isVisible());
      await logout.click({ trial: true });
      await page.evaluate(() => {
        window.revalidationDocument = true;
      });
      if (failure === "http") await route.fulfill({ status: 503 });
      if (failure === "network") await route.abort();
      if (failure === "invalid") await route.fulfill({ json: {} });
      // Let the real AbortSignal timeout fire, without replacing fetch or timers.
      await page.waitForFunction(
        () =>
          !document.documentElement.classList.contains("family-revalidating"),
        null,
        { timeout: 15000 },
      );
      assert.equal(
        await page.evaluate(() => window.familyAccess.verified),
        false,
      );
      assert.equal(
        await page.evaluate(() => window.revalidationDocument),
        true,
      );
      assert.equal(page.url(), base + path);
      assert(await logout.isVisible());
      if (failure === "timeout") await route.abort().catch(() => {});
      await page.unroute(statusURL);
      // A later focus retries successfully without replacing the account page.
      await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      await page.waitForFunction(() => window.familyAccess.verified);
      assert.equal(
        await page.evaluate(() => window.revalidationDocument),
        true,
      );
    }
  }
  await page.route(statusURL, (route) => route.fulfill({ status: 503 }));
  await page.goto(`${base}/about`);
  await page.locator("[data-public-fallback]").waitFor();
  await page.waitForURL(`${base}/about`);
  assert.equal(await page.locator("script[src*='family-session']").count(), 0);
  assert.equal(await page.locator("body[data-family-document]").count(), 0);
  await page.unroute(statusURL);
  console.log(
    "PASS: account controls survive status failures; archive fails closed",
  );
}

export async function checkStaticAccessErrors(page, base) {
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 844 });
    for (const [path, status] of [
      ["/not-a-page", 404],
      ["/access", 503],
    ]) {
      const response = await page.goto(base + path);
      assert.equal(response.status(), status);
      assert.equal(
        await page
          .locator(
            "[data-site-menu], [data-access-link], .family-access-icon, [data-family-document]",
          )
          .count(),
        0,
      );
      assert.equal(
        await page.locator('script[src*="family-session.js"]').count(),
        0,
      );
      assert.equal(
        await page
          .getByRole("link", { name: "Return home", exact: true })
          .getAttribute("href"),
        "/",
      );
      const a11y = await accessibility(page)
        .withTags(["wcag2a", "wcag2aa"])
        .analyze();
      assert.deepEqual(a11y.violations, []);
      await screenshot(page, {
        path: `test-results/auth/error-${path.slice(1)}-${width}.png`,
      });
    }
  }
}
