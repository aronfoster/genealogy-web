import assert from "node:assert/strict";
import { browserInputs } from "./browser_inputs.mjs";
import { accessibility, screenshot } from "./browser_checks.mjs";

// Runs against the existing synthetic HTTPS application and real isolated roles.
export async function checkAuthOutage({ browser, base, routeRequest }) {
  const cases = browserInputs();
  const control = await browser.newContext({ ignoreHTTPSErrors: true });
  const command = async (action) => {
    const response = await control.request.post(`${base}/_fixture/${action}`);
    assert(response.ok(), action);
  };
  const cookie = (value) => ({
    name: "__Host-family-session",
    value,
    url: base,
    secure: true,
    httpOnly: true,
    sameSite: "Lax",
  });
  const publicPage = async (page) => {
    await page.locator("body[data-public-fallback]").waitFor();
    assert.equal(
      await page.locator("[data-family-document], .family-access-icon").count(),
      0,
    );
    assert.equal(
      await page
        .locator("script[src*='listing.js'], script[src*='family-session.js']")
        .count(),
      0,
    );
    assert(!(await page.content()).includes(cases.livingName));
    assert(!(await page.content()).includes(cases.living));
  };
  try {
    // The preceding harness leaves browser-user revoked. Test each state with
    // independent browser cookie jars; provider failures cannot be cached away.
    for (const state of [
      "revoked",
      "approved",
      "pending",
      "anonymous",
      "stale",
    ]) {
      if (state === "approved") await command("reapprove");
      for (const dependency of ["verifier", "authz"]) {
        const outage = dependency === "verifier" ? "outage" : "authz-outage";
        const recover = dependency === "verifier" ? "recover" : "authz-recover";
        const context = await browser.newContext({ ignoreHTTPSErrors: true });
        await context.route("**/*", routeRequest);
        if (state !== "anonymous")
          await context.addCookies([
            cookie(
              state === "stale"
                ? "invalid-cookie"
                : state === "pending"
                  ? "synthetic-new-token"
                  : "synthetic-cookie",
            ),
          ]);
        const page = await context.newPage();
        try {
          await command(outage);
          const fallback =
            state !== "anonymous" &&
            (state !== "stale" || dependency === "verifier");
          for (const path of ["/", "/people?scope=all", "/sources", "/about"]) {
            const response = await page.goto(base + path);
            assert.equal(response.status(), 200);
            assert(response.headers()["cache-control"].includes("no-store"));
            if (fallback) await publicPage(page);
            else
              assert.equal(
                await page.locator("[data-family-document]").count(),
                0,
              );
          }
          const hidden = await page.goto(`${base}/people/${cases.living}`);
          const hiddenBody = await hidden.text();
          const missing = await page.goto(`${base}/people/unknown`);
          assert.equal(hidden.status(), 404);
          assert.equal(missing.status(), 404);
          assert.equal(hiddenBody, await missing.text());
          if (fallback) {
            for (const path of [
              "/api/people",
              `/assets/${cases.asset}`,
              "/access",
            ]) {
              assert.equal(
                (await context.request.get(base + path)).status(),
                503,
              );
            }
            assert(
              (await context.cookies()).some(
                (c) => c.name === "__Host-family-session",
              ),
            );
          }
          await command("public-outage");
          assert.equal((await page.goto(`${base}/people`)).status(), 503);
          assert.equal(await page.locator("[data-access-link]").count(), 0);
          await command("public-recover");
          await command(recover);
          await page.goto(`${base}/people`);
          assert.equal(await page.locator("[data-public-fallback]").count(), 0);
          assert.equal(
            await page.locator("[data-family-document]").count(),
            state === "approved" ? 1 : 0,
          );
          if (state === "stale")
            assert(
              (await context.cookies()).some(
                (c) => c.name === "__Host-family-session",
              ),
            );
        } finally {
          await command(recover);
          await command("public-recover");
          await context.close();
        }
      }
      if (state === "approved") await command("revoke");
    }

    await command("reapprove");
    const context = await browser.newContext({ ignoreHTTPSErrors: true });
    await context.route("**/*", routeRequest);
    await context.addCookies([cookie("synthetic-cookie")]);
    try {
      const page = await context.newPage();
      // Private-only feedback has no public counterpart and must retain the
      // account destination even when only the status API is unavailable.
      await page.route("**/api/access/status", (route) =>
        route.fulfill({ status: 503 }),
      );
      await page.goto(`${base}/feedback`);
      await page.waitForURL(`${base}/access`);
      assert.equal(await page.locator("[data-family-document]").count(), 0);
      await page.unroute("**/api/access/status");
      // Status-only failure must not loop even when document auth is healthy.
      for (const failure of ["http", "network", "malformed", "timeout"]) {
        let release;
        const intercepted = new Promise((resolve) => {
          release = resolve;
        });
        await page.route("**/api/access/status", (route) => release(route));
        await page.goto(
          `${base}/people?q=${encodeURIComponent(cases.query)}&scope=all`,
        );
        const route = await intercepted;
        await page.waitForFunction(() =>
          document.documentElement.classList.contains("family-revalidating"),
        );
        assert.equal(
          await page.locator("body[data-family-document]").isVisible(),
          false,
        );
        if (failure === "http") await route.fulfill({ status: 503 });
        if (failure === "network") await route.abort();
        if (failure === "malformed") await route.fulfill({ json: {} });
        await page
          .locator("[data-public-fallback]")
          .waitFor({ timeout: 15000 });
        await publicPage(page);
        await page.waitForFunction(
          () => !new URL(location.href).searchParams.has("public"),
        );
        assert.equal(new URL(page.url()).searchParams.get("q"), cases.query);
        assert.equal(new URL(page.url()).searchParams.get("scope"), "all");
        if (failure === "timeout") await route.abort().catch(() => {});
        await page.unroute("**/api/access/status");
        // Focus and persisted pageshow of the public fallback do not re-enter
        // the private revalidation controller. Refresh recovers fresh approval.
        await page.evaluate(() => {
          window.outageDocument = true;
          window.dispatchEvent(
            new PageTransitionEvent("pageshow", { persisted: true }),
          );
          window.dispatchEvent(new Event("focus"));
        });
        assert.equal(await page.evaluate(() => window.outageDocument), true);
        await page.reload();
        await page.waitForFunction(
          () => window.familyAccess?.verified === true,
        );
      }
      // Already-open private documents must be concealed on resume, then
      // replaced by public documents for both real dependency failures.
      for (const [outage, recover] of [
        ["outage", "recover"],
        ["authz-outage", "authz-recover"],
      ]) {
        await page.goto(`${base}/people`);
        await page.waitForFunction(() => window.familyAccess?.verified);
        const second = await context.newPage();
        await second.goto(`${base}/people`);
        await second.waitForFunction(() => window.familyAccess?.verified);
        await command(outage);
        await second.evaluate(() =>
          window.dispatchEvent(
            new PageTransitionEvent("pageshow", { persisted: true }),
          ),
        );
        await publicPage(second);
        await page.bringToFront();
        await page.evaluate(() =>
          document.dispatchEvent(new Event("visibilitychange")),
        );
        await publicPage(page);
        for (const width of [1280, 390, 320]) {
          await page.setViewportSize({ width, height: 900 });
          const a11y = await accessibility(page)
            .withTags(["wcag2a", "wcag2aa"])
            .analyze();
          assert.deepEqual(a11y.violations, []);
          await screenshot(page, {
            path: `test-results/auth/public-outage-${outage}-${width}.png`,
            fullPage: true,
          });
        }
        // A CSRF-protected logout works while the provider/database is down;
        // restored public pages never recover the old private DOM after logout.
        const bootstrap = await context.request.get(
          `${base}/api/access/bootstrap`,
        );
        const { csrf } = await bootstrap.json();
        assert.equal(
          (
            await context.request.post(`${base}/api/access/logout`, {
              headers: { Origin: base, "X-CSRF-Token": csrf },
              data: {},
            })
          ).status(),
          200,
        );
        await command(recover);
        await second.bringToFront();
        await second.reload();
        assert.equal(await second.locator("[data-family-document]").count(), 0);
        await second.close();
        await context.addCookies([cookie("synthetic-cookie")]);
      }
    } finally {
      await command("recover");
      await command("authz-recover");
      await command("revoke");
      await context.close();
    }
  } finally {
    await control.close();
  }
  console.log(
    "PASS: public-only auth outage, native authz failure, private DOM replacement and recovery",
  );
}
