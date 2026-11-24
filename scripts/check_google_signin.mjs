// Real pinned Firebase Web SDK + synthetic Google/GAPI transport, real HTTPS/SQL.
// This does not contact Google or establish live-provider/device acceptance.
import assert from "node:assert/strict";
import { devices } from "playwright";
import {
  accessibility,
  screenshot,
  failureScreenshots,
} from "./browser_checks.mjs";

const helperOrigin = "https://synthetic.firebaseapp.com";

// Retain DOM observations outside the document, which navigation will destroy.
export async function watchSignInControls(page) {
  const states = [];
  await page.exposeFunction("recordSignInControls", (state) =>
    states.push(state),
  );
  await page.evaluate(() => {
    const controls = [
      ...document.querySelectorAll("#google-signin, #signin button"),
    ];
    const observer = new MutationObserver(() =>
      window.recordSignInControls(controls.map((button) => button.disabled)),
    );
    for (const control of controls)
      observer.observe(control, {
        attributes: true,
        attributeFilter: ["disabled"],
      });
  });
  return states;
}

// Replace only Google's helper transport. The app's production bundle, SDK
// provider/event manager, signInWithIdp, token refresh and exchange still run.
function syntheticGapi(storageSupported = true) {
  let authEvent;
  window.gapi = {
    load: (name, options) => options.callback(),
    iframes: {
      Iframe: function () {},
      CROSS_ORIGIN_IFRAMES_FILTER: () => true,
      getContext: () => ({
        open: (options, ready) => {
          const frame = document.createElement("iframe");
          frame.src = options.url;
          frame.title = "Synthetic Firebase helper";
          frame.hidden = true;
          options.where.append(frame);
          return ready({
            restyle: async () => {},
            ping: (callback) => Promise.resolve().then(callback),
            register: (name, callback) => {
              if (name === "authEvent") authEvent = callback;
            },
            send: (name, data, callback) => {
              Promise.resolve()
                .then(() => callback([{ [name]: storageSupported }]))
                .catch(() => {});
            },
          });
        },
      }),
    },
  };
  window.addEventListener("message", (event) => {
    if (
      event.origin === "https://synthetic.firebaseapp.com" &&
      event.data?.authEvent
    )
      authEvent?.(event.data);
  });
  const callback = new URL(document.currentScript.src).searchParams.get(
    "onload",
  );
  window[callback]();
}

export async function checkGoogleSignin({
  browser,
  base,
  routeRequest,
  state,
  post,
}) {
  const control = await browser.newContext({ ignoreHTTPSErrors: true });
  await control.route("**/*", routeRequest);
  const operator = await control.newPage();
  await operator.goto(`${base}/access`);
  await state(operator, "reapprove");
  await state(operator, "link");
  await state(operator, "stage-google");

  async function scenario(options, run) {
    const context = await browser.newContext({
      ignoreHTTPSErrors: true,
      ...(options.device || {}),
      ...(options.width
        ? { viewport: { width: options.width, height: 900 } }
        : {}),
    });
    const observed = {
      exchanges: 0,
      refreshes: 0,
      popups: 0,
      emails: 0,
      helperRequests: 0,
      logs: [],
      csp: [],
    };
    const current = {
      token: "synthetic-google-token",
      uid: "browser-user",
      email: "browser@example.com",
      ...options,
    };
    let navigationRoute;
    let intercepted;
    const navigation = new Promise((resolve) => {
      intercepted = resolve;
    });
    await context.exposeBinding("recordCspViolation", (source, url) =>
      observed.csp.push(url),
    );
    await context.addInitScript(() => {
      window.cspViolations = [];
      document.addEventListener("securitypolicyviolation", (event) => {
        window.cspViolations.push(event.blockedURI);
        window.recordCspViolation(event.blockedURI);
      });
    });
    await context.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin === base) {
        if (url.pathname === "/api/access/exchange") {
          observed.exchanges++;
          const data = route.request().postDataJSON();
          assert.equal(data.idToken, current.token);
          assert(route.request().headers()["x-csrf-token"]);
          assert.equal(route.request().headers().origin, base);
          if (current.failExchange)
            return route.fulfill({ status: current.failExchange });
        }
        if (current.holdNavigation && url.pathname === "/me") {
          navigationRoute = route;
          intercepted(route);
          return;
        }
        return routeRequest(route);
      }
      if (url.origin === "https://apis.google.com") {
        observed.helperRequests++;
        // Deliberately leave the script request pending, like a filtering network.
        if (current.hangHelper) return;
        return route.fulfill({
          contentType: "application/javascript",
          body: `(${syntheticGapi})(${!current.storageBlocked});`,
        });
      }
      if (url.origin === helperOrigin) {
        if (url.pathname === "/__/auth/iframe")
          return route.fulfill({
            contentType: "text/html",
            body: "<!doctype html><title>Synthetic helper</title>",
          });
        assert.equal(url.pathname, "/__/auth/handler");
        assert.equal(url.searchParams.get("providerId"), "google.com");
        assert.equal(url.searchParams.get("authType"), "signInViaPopup");
        assert.deepEqual(JSON.parse(url.searchParams.get("customParameters")), {
          prompt: "select_account",
        });
        observed.popups++;
        const event = {
          type: "signInViaPopup",
          eventId: url.searchParams.get("eventId"),
          urlResponse: `${helperOrigin}/__/auth/handler`,
          sessionId: "synthetic-session-id",
        };
        return route.fulfill({
          contentType: "text/html",
          body: `<!doctype html><title>Synthetic Google popup</title><script>
            window.releaseSyntheticGoogle = () => {
              ${current.cancel ? "window.close();" : `opener.postMessage(${JSON.stringify({ authEvent: event })}, ${JSON.stringify(base)});`}
            };
            ${current.holdPopup ? "" : "setTimeout(window.releaseSyntheticGoogle, 100);"}
          </script>`,
        });
      }
      if (url.origin === "https://securetoken.googleapis.com") {
        observed.refreshes++;
        assert.equal(
          new URLSearchParams(route.request().postData()).get("refresh_token"),
          "synthetic-google-refresh",
        );
        return route.fulfill({
          json: {
            access_token: current.token,
            refresh_token: "synthetic-google-refresh",
            expires_in: "3600",
            user_id: current.uid,
          },
        });
      }
      if (url.origin === "https://identitytoolkit.googleapis.com") {
        if (url.pathname.endsWith("/accounts:sendOobCode")) observed.emails++;
        if (url.pathname.endsWith("/projects"))
          return route.fulfill({
            json: {
              authorizedDomains: current.unauthorized ? [] : ["127.0.0.1"],
            },
          });
        if (url.pathname.endsWith("/accounts:signInWithIdp")) {
          const data = route.request().postDataJSON();
          assert.equal(data.returnSecureToken, true);
          assert.equal(data.requestUri, `${helperOrigin}/__/auth/handler`);
          assert(!data.idToken, "This must be sign-in, never provider linking");
          if (current.collision)
            return route.fulfill({
              json: {
                needConfirmation: true,
                email: "private-collision@example.com",
                pendingToken: "private-collision-credential",
              },
            });
          if (current.providerDisabled)
            return route.fulfill({
              status: 400,
              json: { error: { message: "OPERATION_NOT_ALLOWED" } },
            });
          return route.fulfill({
            json: {
              localId: current.uid,
              email: current.email,
              emailVerified: true,
              providerId: "google.com",
              idToken: current.token,
              refreshToken: "synthetic-google-refresh",
              expiresIn: "3600",
              oauthAccessToken: "private-google-oauth-token",
            },
          });
        }
        if (url.pathname.endsWith("/accounts:lookup"))
          return route.fulfill({
            json: {
              users: [
                {
                  localId: current.uid,
                  email: current.email,
                  emailVerified: true,
                  providerUserInfo: [
                    { providerId: "google.com", rawId: "synthetic-google-id" },
                  ],
                },
              ],
            },
          });
      }
      return routeRequest(route);
    });
    const page = await context.newPage();
    if (current.hangHelper) await page.clock.install();
    page.on("console", (message) => observed.logs.push(message.text()));
    page.on("pageerror", (error) => observed.logs.push(error.message));
    try {
      const helperRequested = current.hangHelper
        ? page.waitForRequest("https://apis.google.com/**")
        : null;
      await page.goto(
        `${base}/access${current.next ? `?next=${encodeURIComponent(current.next)}` : ""}`,
        { waitUntil: current.hangHelper ? "domcontentloaded" : "load" },
      );
      const google = page.getByRole("button", {
        name: "Continue with Google",
        exact: true,
      });
      await google.waitFor();
      if (helperRequested) await helperRequested;
      else
        await page.waitForFunction(
          () => !document.querySelector("#google-signin").disabled,
        );
      assert.deepEqual(await page.evaluate(() => window.cspViolations), []);
      await run({ context, page, google, current, observed, navigation });
      assert.deepEqual(
        observed.csp,
        [],
        "Google helper violated the actual page CSP",
      );
      const stored = await page.evaluate(() => ({
        local: { ...localStorage },
        session: { ...sessionStorage },
      }));
      const publicOutput = JSON.stringify(stored) + observed.logs.join(" ");
      for (const privateValue of [
        current.email,
        current.token,
        "synthetic-google-refresh",
        "private-google-oauth-token",
        "private-collision@example.com",
        "private-collision-credential",
      ])
        assert(
          !publicOutput.includes(privateValue),
          "Credentials leaked into browser storage/logs",
        );
    } catch (error) {
      console.error("Google scenario failed:", {
        device: options.device?.defaultBrowserType || "desktop",
        width: options.width,
        exchanges: observed.exchanges,
        refreshes: observed.refreshes,
        popups: observed.popups,
        result: await page
          .locator("#access-result")
          .textContent()
          .catch(() => "unavailable"),
      });
      await failureScreenshots(browser, "test-results/auth");
      throw error;
    } finally {
      await navigationRoute?.abort().catch(() => {});
      await context.close();
    }
  }

  try {
    for (const device of [devices["Pixel 7"], devices["iPhone SE"]]) {
      await scenario(
        { device, hangHelper: true },
        async ({ page, google, observed }) => {
          assert.equal(observed.helperRequests, 1);
          assert(await google.isDisabled());
          const started = performance.now();
          await page.locator("#email").fill("browser@example.com");
          await page
            .getByRole("button", { name: "Email me a sign-in link" })
            .click();
          await page
            .getByText("If a sign-in link can be sent", { exact: false })
            .waitFor({ timeout: 5000 });
          assert(
            performance.now() - started < 5000,
            "Email fallback waited for Google's helper",
          );
          assert.equal(observed.emails, 1);
          assert.equal(observed.exchanges, 0);
          assert(await google.isDisabled());
          // Advance only browser timers; no minute-long SDK timeout in the suite.
          await page.clock.runFor(16000);
          await page
            .getByText("Google sign-in is unavailable", { exact: false })
            .waitFor();
          assert(await google.isDisabled());
          assert(!(await page.locator("#signin button").isDisabled()));
        },
      );
    }
    for (const options of [
      { width: 1280, holdNavigation: true },
      { device: devices["Pixel 7"], next: "/sources" },
      { device: devices["iPhone SE"], width: 320 },
    ]) {
      await scenario(
        options,
        async ({ page, google, observed, current, navigation }) => {
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
            path: `test-results/auth/google-${options.width || 390}.png`,
            fullPage: true,
          });
          current.holdPopup = true;
          const controls = current.holdNavigation
            ? await watchSignInControls(page)
            : null;
          const popupReady = page.waitForEvent("popup");
          await google.focus();
          await page.keyboard.press("Enter");
          const popup = await popupReady;
          await popup.waitForFunction(() => !!window.releaseSyntheticGoogle);
          assert(await page.locator("#signin button").isDisabled());
          assert(await google.isDisabled());
          await popup.evaluate(() => window.releaseSyntheticGoogle());
          if (current.holdNavigation) {
            const route = await navigation;
            // Leave the old document alive while the successful destination waits.
            assert(controls.length > 0);
            assert(controls.every((state) => state.every(Boolean)));
            await route.continue();
          }
          await page.waitForURL(
            current.next ? `${base}${current.next}` : /\/people\/per_/,
          );
          if (controls)
            assert(
              controls.every((state) => state.every(Boolean)),
              "Controls re-enabled during successful navigation",
            );
          assert.equal(observed.exchanges, 1);
          assert.equal(observed.refreshes, 1);
          assert.equal(observed.popups, 1);
          await page
            .locator("body[data-family-document]")
            .waitFor({ state: "visible" });
          await page.goto(`${base}/access`);
          await page
            .getByRole("button", { name: "Sign out", exact: true })
            .click();
          await page.locator("#signin").waitFor();
          assert(
            !(await page.context().cookies()).some(
              (c) => c.name === "__Host-family-session",
            ),
          );
        },
      );
    }
    for (const options of [
      { cancel: true, message: "was cancelled" },
      { blocked: true, message: "Allow the Google sign-in popup" },
      { collision: true, message: "could not finish" },
      { unauthorized: true, message: "could not finish" },
      { providerDisabled: true, message: "could not finish" },
      { storageBlocked: true, message: "could not finish" },
      { failExchange: 503, message: "could not finish" },
      { failExchange: 429, message: "Too many sign-in attempts" },
    ]) {
      await scenario(options, async ({ page, google, current, observed }) => {
        if (current.blocked)
          await page.evaluate(() => {
            window.open = () => null;
          });
        await google.click();
        await page.getByText(current.message, { exact: false }).waitFor();
        await page.waitForFunction(
          () => !document.querySelector("#google-signin").disabled,
        );
        assert.equal(observed.exchanges, current.failExchange ? 1 : 0);
        assert(!(await page.textContent("body")).includes("private-collision"));
        assert(
          !(await page.context().cookies()).some(
            (c) => c.name === "__Host-family-session",
          ),
        );
        assert.equal(new URL(page.url()).pathname, "/access");
        // A provider failure leaves the email-link option usable on this page.
        await page.locator("#email").fill("browser@example.com");
        await page
          .getByRole("button", { name: "Email me a sign-in link" })
          .click();
        await page
          .getByText("If a sign-in link can be sent", { exact: false })
          .waitFor();
        if (current.collision || current.failExchange) {
          current.collision = false;
          current.failExchange = false;
          await google.click();
          await page.waitForURL(/\/people\/per_/);
          assert.equal(observed.exchanges, options.failExchange ? 2 : 1);
        }
      });
    }
    for (const identity of [
      {
        token: "synthetic-new-token",
        uid: "new-user",
        email: "new@example.com",
        expected: "pending",
      },
      {
        token: "synthetic-collision-token",
        uid: "changed-uid",
        expected: "pending",
      },
      {
        token: "synthetic-preauth-token",
        uid: "preauth-user",
        email: "preauth@example.com",
        expected: "approved",
      },
    ]) {
      await scenario(identity, async ({ page, google, current }) => {
        await google.click();
        if (current.expected === "approved") {
          await page.waitForURL(/\/people\/per_/);
        } else {
          await page.locator('[data-access-state="pending"]').waitFor();
          assert.equal(await page.locator("[data-family-document]").count(), 0);
          assert.deepEqual(
            await page.evaluate(async () =>
              (await fetch("/api/access/status")).json(),
            ),
            { allowed: false },
          );
        }
        assert(!(await page.textContent("body")).includes(current.email));
      });
    }
    await state(operator, "revoke");
    await scenario({}, async ({ page, google }) => {
      await google.click();
      await page.locator('[data-access-state="revoked"]').waitFor();
      assert.equal(await page.locator("[data-family-document]").count(), 0);
      assert.deepEqual(
        await post(page, "exchange", { idToken: "synthetic-google-token" }),
        { status: 200, body: { next: "/access" } },
      );
    });
    console.log(
      "Google popup SDK/refresh/exchange, same UID, staged/unknown/conflicted/revoked access, desktop/mobile layouts, cancellation/blocking/provider errors, rate limits, navigation guards, storage privacy, logout and email fallback with a stalled mobile/Safari helper passed (synthetic provider transport).",
    );
  } finally {
    await control.close();
  }
}
