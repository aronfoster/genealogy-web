import { checkFeedback } from "./check_feedback.mjs";
import { checkAuthOutage } from "./check_auth_outage.mjs";
import {
  checkAccessAppearance,
  checkAccessRevalidation,
  checkStaticAccessErrors,
} from "./check_navigation.mjs";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { chromium } from "playwright";
import packagedChromium from "@sparticuz/chromium";
import {
  checkGoogleSignin,
  watchSignInControls,
} from "./check_google_signin.mjs";
import {
  accessibility,
  screenshot,
  failureScreenshots,
} from "./browser_checks.mjs";

const base = process.env.AUTH_TEST_BASE_URL;
const browser = await chromium.launch({
  executablePath: await packagedChromium.executablePath(),
  // The package defaults disable web security, suppressing real Origin behavior.
  // Auth acceptance must exercise the browser's actual origin and cookie rules.
  args: packagedChromium.args.filter(
    (arg) =>
      ![
        "--single-process",
        "--disable-web-security",
        "--allow-running-insecure-content",
        "--disable-site-isolation-trials",
      ].includes(arg) && !arg.startsWith("--disable-features="),
  ),
});
const context = await browser.newContext({ ignoreHTTPSErrors: true });
let sentContinueURL;
const routeRequest = async (route) => {
  const url = new URL(route.request().url());
  if (url.origin === base) return route.continue();
  if (url.origin === "https://securetoken.googleapis.com") {
    assert.equal(
      new URLSearchParams(route.request().postData()).get("refresh_token"),
      "synthetic-refresh",
    );
    return route.fulfill({
      json: {
        access_token: "synthetic-token",
        refresh_token: "synthetic-refresh",
        expires_in: "3600",
        user_id: "browser-user",
      },
    });
  }
  if (url.origin === "https://identitytoolkit.googleapis.com") {
    const data = route.request().postDataJSON();
    if (url.pathname.endsWith("/accounts:sendOobCode")) {
      sentContinueURL = data.continueUrl;
      return route.fulfill({ json: { email: "browser@example.com" } });
    }
    if (url.pathname.endsWith("/accounts:lookup")) {
      return route.fulfill({
        json: {
          users: [
            {
              localId: "browser-user",
              email: "browser@example.com",
              emailVerified: true,
              providerUserInfo: [
                {
                  providerId: "password",
                  email: "browser@example.com",
                  rawId: "browser@example.com",
                },
              ],
            },
          ],
        },
      });
    }
    if (url.pathname.endsWith("/accounts:signInWithEmailLink")) {
      assert.equal(data.email, "browser@example.com");
      return route.fulfill({
        json: {
          localId: "browser-user",
          email: "browser@example.com",
          idToken: "synthetic-token",
          refreshToken: "synthetic-refresh",
          expiresIn: "3600",
        },
      });
    }
  }
  return route.abort();
};
await context.route("**/*", routeRequest);
await fs.mkdir("test-results/auth", { recursive: true });
const post = async (page, action, data = {}) =>
  page.evaluate(
    async ({ action, data }) => {
      const { csrf } = await (await fetch("/api/access/bootstrap")).json();
      const response = await fetch(`/api/access/${action}`, {
        method: "POST",
        referrerPolicy: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify(data),
      });
      return { status: response.status, body: await response.json() };
    },
    { action, data },
  );
const state = async (page, action) =>
  page.evaluate(async (action) => {
    const response = await fetch(`/_fixture/${action}`, { method: "POST" });
    if (!response.ok) throw new Error("fixture transition failed");
  }, action);
try {
  const page = await context.newPage();
  const anonymousBoxes = await checkAccessAppearance(page, base, "anonymous");
  assert.equal(
    (await post(page, "exchange", { idToken: "synthetic-token" })).status,
    200,
  );
  assert.deepEqual(
    await checkAccessAppearance(page, base, "pending"),
    anonymousBoxes,
  );
  await page.locator("#explanation").fill("Synthetic family connection");
  await page.getByRole("button", { name: "Request review" }).click();
  await page
    .getByText("Your request is waiting for review.", { exact: false })
    .waitFor();
  await state(page, "approve");
  assert.deepEqual(
    await checkAccessAppearance(page, base, "approved"),
    anonymousBoxes,
  );
  await checkAccessRevalidation(page, base);
  await page.goto(`${base}/me`);
  await page
    .getByText("Your profile is not available here yet.", { exact: false })
    .waitFor();
  await screenshot(page, {
    path: "test-results/auth/my-profile-unlinked.png",
    fullPage: true,
  });
  await state(page, "link");
  const approved = await post(page, "exchange", { idToken: "synthetic-token" });
  assert.equal(approved.body.next, "/me");
  await page.goto(`${base}/me`);
  assert(new URL(page.url()).pathname.startsWith("/people/per_"));
  const profile = page.url();
  await checkFeedback(context, page, base, new URL(profile).pathname);
  await page.goto(base);
  await page.locator("[data-site-menu] summary").click();
  await page.getByRole("link", { name: "My profile", exact: true }).click();
  await page.waitForURL(profile);
  await page.locator("[data-site-menu] summary").click();
  await page
    .getByRole("link", { name: "Family access active", exact: true })
    .click();
  assert.equal(
    new URL(page.url()).searchParams.get("next"),
    new URL(profile).pathname,
  );
  // Settle the account page's initial check before invalidating its session.
  // Otherwise its denial-triggered reload can abort the next test navigation.
  await page.waitForFunction(() => window.familyAccess?.verified === true);
  // A fresh browser receives the actual SDK's continue URL; no shared storage.
  await post(page, "logout");
  await page.goto(`${base}/access?next=/sources`);
  await page.locator("#email").fill("browser@example.com");
  await page.getByRole("button", { name: "Email me a sign-in link" }).click();
  await page
    .getByText("If a sign-in link can be sent", { exact: false })
    .waitFor();
  assert.equal(new URL(sentContinueURL).searchParams.get("next"), "/sources");
  const otherDevice = await browser.newContext({ ignoreHTTPSErrors: true });
  let navigationRoute;
  try {
    await otherDevice.route("**/*", routeRequest);
    const callback = new URL(sentContinueURL);
    callback.searchParams.set("apiKey", "synthetic-key");
    callback.searchParams.set("mode", "signIn");
    callback.searchParams.set("oobCode", "synthetic-code");
    const receiver = await otherDevice.newPage();
    await receiver.goto(callback.href);
    await receiver.waitForURL(`${base}/access/complete`);
    assert.equal(await receiver.evaluate(() => localStorage.length), 0);
    let intercepted;
    const navigating = new Promise((resolve) => {
      intercepted = resolve;
    });
    await receiver.route(`${base}/sources`, (route) => {
      navigationRoute = route;
      intercepted();
    });
    await receiver.locator("#email").fill("browser@example.com");
    const controls = await watchSignInControls(receiver);
    await receiver
      .getByRole("button", { name: "Finish signing in" })
      .click({ noWaitAfter: true });
    await navigating;
    assert(controls.length > 0);
    assert(controls.every((state) => state.every(Boolean)));
    await navigationRoute.continue();
    await receiver.waitForURL(`${base}/sources`);
    assert(
      controls.every((state) => state.every(Boolean)),
      "Email controls re-enabled during successful navigation",
    );
    await receiver
      .locator("body[data-family-document]")
      .waitFor({ state: "visible" });
  } finally {
    await navigationRoute?.abort().catch(() => {});
    await otherDevice.close().catch(() => {});
  }
  await post(page, "exchange", { idToken: "synthetic-token" });
  await page.goto(base);
  await page
    .locator("body[data-family-document]")
    .waitFor({ state: "visible" });
  const second = await context.newPage();
  await second.goto(base);
  await second
    .locator("body[data-family-document]")
    .waitFor({ state: "visible" });
  const account = await context.newPage();
  await account.goto(`${base}/access?next=%2Fpeople`);
  await account.waitForFunction(() => window.familyAccess.verified);
  await page.bringToFront();
  await page.locator("[data-site-menu] summary").click();
  await page
    .getByRole("navigation", { name: "Main", exact: true })
    .getByRole("link", { name: "Family access active", exact: true })
    .click();
  await page.waitForURL(`${base}/access`);
  // Account navigation has no side effect; sign out remains a separate action.
  assert.equal(
    await page.locator("[data-access-link]").getAttribute("aria-current"),
    "page",
  );
  // The initial check must settle before focus can start another validation.
  // Otherwise production request coalescing correctly ignores that focus event.
  await page.waitForFunction(() => window.familyAccess?.verified === true);
  let failedStatusRequests = 0;
  await page.route("**/api/access/status", (route) => {
    failedStatusRequests += 1;
    return route.fulfill({ status: 503 });
  });
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await page.waitForFunction(
    () =>
      !window.familyAccess.verified &&
      !document.documentElement.classList.contains("family-revalidating"),
  );
  assert(
    failedStatusRequests > 0,
    "Revalidation must encounter the injected 503",
  );
  await page.getByRole("button", { name: "Sign out", exact: true }).focus();
  await page.keyboard.press("Enter");
  await page.locator("#signin").waitFor();
  await page.unroute("**/api/access/status");
  await account.bringToFront();
  await account.locator("#signin").waitFor();
  assert.equal(account.url(), `${base}/access?next=%2Fpeople`);
  assert.equal(
    await account.locator("[data-access-link]").textContent(),
    "Sign in for family access",
  );
  await account.close();
  await second.bringToFront();
  await second.waitForURL(`${base}/access`);
  assert.equal(await second.locator("[data-family-document]").count(), 0);
  assert.equal(
    (await post(page, "exchange", { idToken: "synthetic-token" })).status,
    200,
  );
  await page.goto(base);
  await page
    .locator("body[data-family-document]")
    .waitFor({ state: "visible" });
  await state(page, "revoke");
  await page.evaluate(() =>
    window.dispatchEvent(
      new PageTransitionEvent("pageshow", { persisted: true }),
    ),
  );
  await page.waitForURL(`${base}/access`);
  assert.equal(await page.locator("[data-family-document]").count(), 0);
  assert.deepEqual(
    await checkAccessAppearance(page, base, "revoked"),
    anonymousBoxes,
  );
  await state(page, "outage");
  await checkStaticAccessErrors(page, base);
  await state(page, "recover");
  await state(page, "reapprove");
  await page.goto(base);
  await page.goto(`${base}/access`);
  await state(page, "revoke");
  await page.goBack();
  // A real back navigation may refetch the now-public document instead of
  // restoring bfcache; either outcome must discard the family DOM.
  await page.waitForFunction(
    () => !document.querySelector("[data-family-document]"),
  );
  await page.goto(`${base}/access`);
  for (const width of [1280, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    const a11y = await accessibility(page)
      .withTags(["wcag2a", "wcag2aa"])
      .analyze();
    assert.deepEqual(
      a11y.violations.map((v) => v.id),
      [],
    );
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    await screenshot(page, {
      path: `test-results/auth/revoked-${width}.png`,
      fullPage: true,
    });
  }
  console.log(
    "HTTPS family login/request, cross-tab logout, persisted pageshow, back navigation, and mobile accessibility passed.",
  );
  await checkAuthOutage({ browser, base, routeRequest });
  await checkGoogleSignin({ browser, base, routeRequest, state, post });
} catch (error) {
  await failureScreenshots(browser, "test-results/auth");
  throw error;
} finally {
  await context.close().catch(() => {});
  await browser.close();
}
