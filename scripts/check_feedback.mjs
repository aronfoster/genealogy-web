import assert from "node:assert/strict";
import { accessibility, screenshot } from "./browser_checks.mjs";

// Called only by the synthetic HTTPS auth harness, with real CSRF/membership
// checks, a fake YouTrack REST transport, and a real disposable intent ledger. No real feedback or API credential.
export async function checkFeedback(context, page, base, profile) {
  const status = page.locator(".feedback-status");
  const message = page.getByRole("textbox", { name: "Tell us more" });
  const send = () => page.locator('.feedback-form button[type="submit"]');
  const next = () =>
    page.getByRole("button", { name: "Send another suggestion" });
  const checkOverflow = async () =>
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
  const counts = async () =>
    (await context.request.post(`${base}/_fixture/feedback-count`)).json();
  let seen = [];
  const observe = (request) => {
    if (request.url() === `${base}/api/feedback`)
      seen.push(request.postDataJSON());
  };
  page.on("request", observe);
  const submit = async () => {
    await send().focus();
    await page.keyboard.press("Enter");
  };
  const success = async () => {
    await next().waitFor({ state: "visible" });
    assert.match(
      await status.textContent(),
      /^Thank you\. Reference [A-F0-9]{4}-[A-F0-9]{4}\. Your suggestion has been received for review; nothing changes automatically\.$/,
    );
    assert(await status.evaluate((node) => node === document.activeElement));
  };
  try {
    for (const width of [390, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(base + profile);
      await page
        .locator("body[data-family-document]")
        .waitFor({ state: "visible" });
      const menu = page.locator("[data-site-menu]");
      await menu.locator("summary").focus();
      await page.keyboard.press("Enter");
      assert(await menu.evaluate((node) => node.open));
      const link = menu.getByRole("link", {
        name: "Suggest a change",
        exact: true,
      });
      const box = await link.boundingBox();
      assert(box.height >= 44);
      assert.equal(
        await link.getAttribute("href"),
        `/feedback?from=${encodeURIComponent(profile)}`,
      );
      assert(
        await link.evaluate(
          (node) => node.nextElementSibling.textContent === "My profile",
        ),
      );
      await link.focus();
      await page.keyboard.press("Enter");
      await page.waitForURL(/\/feedback\?/);
      await send().waitFor({ state: "visible" });
      assert(await page.locator('[value="correction"]').isChecked());
      const include = page.getByRole("checkbox", {
        name: "Include the page I'm viewing",
      });
      assert(await include.isChecked());
      await page.locator('[value="correction"]').focus();
      await page.keyboard.press("ArrowDown");
      assert(await page.locator('[value="add_person"]').isChecked());
      await page.keyboard.press("Tab");
      assert(await message.evaluate((node) => node === document.activeElement));
      await page.keyboard.type("Synthetic family relationship suggestion.");
      assert.match(
        await page.locator("#feedback-helper").textContent(),
        /parent for a minor.*signed-in archive.*reviewed/,
      );
      await page.locator('[value="add_person"]').focus();
      await page.keyboard.press("ArrowDown");
      assert.match(
        await page.locator("#feedback-helper").textContent(),
        /hard to understand/,
      );
      assert.equal(
        await message.inputValue(),
        "Synthetic family relationship suggestion.",
      );
      await checkOverflow();
      const result = await accessibility(page)
        .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
        .analyze();
      assert.deepEqual(result.violations, [], `feedback axe ${width}`);
      await screenshot(page, {
        path: `test-results/auth/feedback-${width}.png`,
        fullPage: true,
      });
      const before = await counts();
      await submit();
      await success();
      assert.equal((await counts()).count, before.count + 1);
      assert.equal(seen.at(-1).pagePath, profile);
      const oldKey = seen.at(-1).clientKey;
      await checkOverflow();
      await next().focus();
      await page.keyboard.press("Enter");
      assert.equal(await message.inputValue(), "");
      assert(await page.locator('[value="correction"]').isChecked());
      assert(await include.isChecked());
      await include.focus();
      await page.keyboard.press("Space");
      // One complete second submission covers the reset contract, keeping this
      // shared synthetic identity within the production 10-request window.
      if (width === 390) {
        await message.focus();
        await message.fill("line one\tcolumn\nline two\u0007");
        await submit();
        await success();
        assert.equal(seen.at(-1).message, "line one column\nline two");
        assert.equal(await message.inputValue(), seen.at(-1).message);
        assert.equal(seen.at(-1).pagePath, null);
        assert.notEqual(seen.at(-1).clientKey, oldKey);
      }
    }

    await page.goto(`${base}/feedback`);
    assert.equal(await page.getByRole("checkbox").count(), 0);
    await page.goto(`${base}/feedback?from=${encodeURIComponent(profile)}`);
    await message.fill("Synthetic retry <img src=x onerror=alert(1)>.");
    // Both cases commit at the fake provider before dropping the HTTP receipt.
    // Retrying must reconcile the original UUID, not create another issue.
    for (const failure of ["html504", "network"]) {
      await page.locator('[value="add_person"]').check();
      const include = page.getByRole("checkbox", {
        name: "Include the page I'm viewing",
      });
      if (failure === "network") await include.uncheck();
      const withContext = await include.isChecked();
      const before = await counts();
      const start = seen.length;
      let release;
      const gate = new Promise((resolve) => {
        release = resolve;
      });
      let arrived;
      let timeout;
      const dispatched = new Promise((resolve, reject) => {
        arrived = resolve;
        timeout = setTimeout(
          () =>
            reject(new Error("Feedback did not reach the synthetic provider")),
          30000,
        );
      });
      const handler = async (route) => {
        await route.fetch();
        arrived();
        await gate;
        if (failure === "html504")
          await route.fulfill({
            status: 504,
            contentType: "text/html",
            body: "<html>Gateway timeout</html>",
          });
        else await route.abort("failed");
      };
      await page.route("**/api/feedback", handler);
      try {
        await submit();
        await dispatched;
        assert(await send().isDisabled());
        assert.equal(await send().textContent(), "Sending…");
        await page.evaluate(() => {
          const form = document.querySelector("[data-feedback-form]");
          form.dispatchEvent(
            new Event("submit", { bubbles: true, cancelable: true }),
          );
        });
        release();
        await page.waitForFunction(() =>
          document
            .querySelector(".feedback-status")
            .textContent.includes("couldn't confirm delivery"),
        );
      } finally {
        release();
        clearTimeout(timeout);
        await page.unroute("**/api/feedback", handler);
      }
      assert.equal(
        await message.inputValue(),
        "Synthetic retry <img src=x onerror=alert(1)>.",
      );
      assert.equal(await message.getAttribute("readonly"), "");
      assert(await send().evaluate((node) => node === document.activeElement));
      assert(await page.locator('[value="add_person"]').isChecked());
      assert.equal(await include.isChecked(), withContext);
      assert.equal(seen.length, start + 1);
      // A later refusal says nothing about the already-committed first attempt.
      for (const code of [400, 401, 403, 429]) {
        const refused = (route) =>
          route.fulfill({ status: code, json: { error: "Refused" } });
        await page.route("**/api/feedback", refused);
        try {
          await submit();
          await page.waitForFunction(
            () => !document.querySelector("form[aria-busy]"),
          );
          assert(await message.evaluate((node) => node.readOnly));
          assert(await page.locator(".feedback-retry").isVisible());
          assert.deepEqual(seen.at(-1), seen[start]);
          assert.equal((await counts()).count, before.count + 1);
        } finally {
          await page.unroute("**/api/feedback", refused);
        }
      }
      await submit();
      await success();
      assert.deepEqual(seen[start], seen.at(-1));
      assert.equal((await counts()).count, before.count + 1);
      assert.equal((await counts()).calls, before.calls + 2);
      await next().click();
      await message.fill("Synthetic retry <img src=x onerror=alert(1)>.");
    }
    // A provider commit whose receipt is lost plus a stale index stays pending.
    // The real ledger must permit only GET reconciliation, even on repeated 202s.
    // Advance the injected clock for the additional scenarios; keep the real
    // 10-per-600s limit unchanged (native tests cover exact enforcement).
    await context.request.post(`${base}/_fixture/next-rate-window`);
    const pendingBefore = await counts();
    const pendingStart = seen.length;
    await context.request.post(`${base}/_fixture/feedback-pending`);
    await submit();
    await page.waitForFunction(() =>
      document
        .querySelector(".feedback-status")
        .textContent.includes("may have been received"),
    );
    assert(await message.evaluate((node) => node.readOnly));
    assert(!(await status.textContent()).includes("wasn't sent"));
    await submit();
    await page.waitForFunction(
      () => !document.querySelector("form[aria-busy]"),
    );
    assert((await status.textContent()).includes("may have been received"));
    assert.equal((await counts()).count, pendingBefore.count + 1);
    assert.deepEqual(seen[pendingStart], seen.at(-1));
    await context.request.post(`${base}/_fixture/feedback-resolve`);
    await submit();
    await success();
    assert.equal((await counts()).count, pendingBefore.count + 1);
    assert.deepEqual(seen[pendingStart], seen.at(-1));
    await next().click();
    await message.fill("Synthetic retry <img src=x onerror=alert(1)>.");
    // A definite rejection has different copy but still retains the bound payload.
    const rejected = (route) =>
      route.fulfill({ status: 503, json: { delivery: "rejected" } });
    await page.route("**/api/feedback", rejected);
    await submit();
    await page.waitForFunction(() =>
      document
        .querySelector(".feedback-status")
        .textContent.includes("wasn't sent yet"),
    );
    assert(await message.evaluate((node) => node.readOnly));
    await page.unroute("**/api/feedback", rejected);
    await submit();
    await success();
    await next().click();
    await message.fill("Synthetic retry <img src=x onerror=alert(1)>.");
    // Provider errors and rate limiting keep the same payload/key too.
    for (const code of [503, 429]) {
      const handler = (route) =>
        route.fulfill({
          status: code,
          json: { error: "<img src=x onerror=alert(1)>" },
        });
      const start = seen.length;
      await page.route("**/api/feedback", handler);
      await submit();
      await page.waitForFunction(
        (code) =>
          document
            .querySelector(".feedback-status")
            .textContent.includes(
              code === 429 ? "several messages" : "couldn't confirm delivery",
            ),
        code,
      );
      await page.unroute("**/api/feedback", handler);
      assert.equal(await status.locator("img").count(), 0);
      await submit();
      await success();
      assert.deepEqual(seen[start], seen[start + 1]);
      await next().click();
      await message.fill("Synthetic retry <img src=x onerror=alert(1)>.");
    }
    // A forged success body cannot inject markup or erase the pending message.
    const forged = (route) =>
      route.fulfill({
        json: { reference: "<img src=x onerror=alert(1)>", issue: "GEN-999" },
      });
    await page.route("**/api/feedback", forged);
    await submit();
    await page.waitForFunction(() =>
      document
        .querySelector(".feedback-status")
        .textContent.includes("couldn't confirm delivery"),
    );
    assert.equal(await status.locator("img").count(), 0);
    await page.unroute("**/api/feedback", forged);
    await submit();
    await success();
    // First-attempt refusals allow correction/sign-in without discarding a draft
    // or rotating its key. These intercepted requests do not use the rate budget.
    for (const code of [400, 401, 403]) {
      await next().click();
      await message.fill("Synthetic refused draft.");
      const start = seen.length;
      const refused = (route) =>
        route.fulfill({ status: code, json: { error: "<img src=x>" } });
      await page.route("**/api/feedback", refused);
      try {
        await submit();
        await page.waitForFunction(
          () => !document.querySelector("form[aria-busy]"),
        );
        assert.equal(await message.inputValue(), "Synthetic refused draft.");
        assert.equal(await message.evaluate((node) => node.readOnly), false);
        assert(await page.locator(".feedback-retry").isHidden());
        assert.equal(await status.locator("img").count(), 0);
        assert.match(
          await status.textContent(),
          code === 400
            ? /check your message/
            : code === 401
              ? /sign in again.*return here/
              : /access couldn't be verified/,
        );
        const signin = page.locator("[data-feedback-signin]");
        assert.equal(await signin.isVisible(), code === 401);
        if (code === 401) {
          const popup = page.waitForEvent("popup");
          await signin.getByRole("link").click();
          const access = await popup;
          assert.equal(
            await signin.getByRole("link").getAttribute("href"),
            "/access?next=/feedback",
          );
          await access.close();
          assert.equal(await message.inputValue(), "Synthetic refused draft.");
        }
      } finally {
        await page.unroute("**/api/feedback", refused);
      }
      await message.fill("Synthetic corrected draft.");
      const accepted = (route) =>
        route.fulfill({ json: { reference: "ABCD-1234", issue: "GEN-999" } });
      await page.route("**/api/feedback", accepted);
      try {
        await submit();
        await success();
        assert.equal(seen.at(-1).clientKey, seen[start].clientKey);
        assert.equal(seen.at(-1).message, "Synthetic corrected draft.");
      } finally {
        await page.unroute("**/api/feedback", accepted);
      }
    }
    // A conflict never silently starts a second issue, whether or not an earlier
    // attempt may already have committed. Administrator reconciliation is needed.
    for (const uncertain of [false, true]) {
      if (uncertain) await page.goto(`${base}/feedback`);
      else await next().click();
      await message.fill("Synthetic conflicting draft.");
      const start = seen.length;
      let code = uncertain ? 503 : 409;
      const refused = (route) =>
        route.fulfill({ status: code, json: { error: "Reference conflict" } });
      await page.route("**/api/feedback", refused);
      try {
        await submit();
        await page.waitForFunction(
          () => !document.querySelector("form[aria-busy]"),
        );
        if (uncertain) {
          code = 409;
          await submit();
          await page.waitForFunction(
            () =>
              document.querySelector('.feedback-form button[type="submit"]')
                .disabled && !document.querySelector("form[aria-busy]"),
          );
          assert.deepEqual(seen[start], seen.at(-1));
        }
        assert.match(
          await status.textContent(),
          /contact the archive administrator before sending/,
        );
        assert(await send().isDisabled());
        assert.equal(
          await message.inputValue(),
          "Synthetic conflicting draft.",
        );
        assert(await page.locator(".feedback-retry").isHidden());
        assert(await next().isHidden());
        assert(
          await status.evaluate((node) => node === document.activeElement),
        );
        const attempts = seen.length;
        await page.evaluate(() =>
          document
            .querySelector("[data-feedback-form]")
            .dispatchEvent(
              new Event("submit", { bubbles: true, cancelable: true }),
            ),
        );
        assert.equal(seen.length, attempts);
      } finally {
        await page.unroute("**/api/feedback", refused);
      }
    }
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    const source = await (
      await context.request.get(`${base}/static/feedback.js`)
    ).text();
    assert(!source.includes("innerHTML") && !source.includes("localStorage"));
    console.log(
      "PASS: feedback mobile/desktop, keyboard, axe, context, fresh suggestion, paste cleanup, refusal/sign-in recovery, conflict guidance, double send, 503/504/network same-UUID reconciliation, status XSS and no storage",
    );
  } finally {
    page.off("request", observe);
  }
}
