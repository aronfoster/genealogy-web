import assert from "node:assert/strict";
import { chromium } from "playwright";
import binary from "@sparticuz/chromium";
import AxeBuilder from "@axe-core/playwright";
import { accessibility } from "../scripts/browser_checks.mjs";

const [owner, reader] = process.argv.slice(2);
const browser = await chromium.launch({
  executablePath: await binary.executablePath(),
  args: binary.args.filter(
    (a) =>
      ![
        "--single-process",
        "--disable-web-security",
        "--allow-running-insecure-content",
        "--disable-site-isolation-trials",
      ].includes(a) && !a.startsWith("--disable-features="),
  ),
});
try {
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  const member = await ctx.newPage();
  await member.goto(reader + "/access");
  await member.evaluate(async () => {
    const { csrf } = await (await fetch("/api/access/bootstrap")).json();
    const res = await fetch("/api/access/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ idToken: "synthetic-token" }),
    });
    if (!res.ok) throw Error("reader exchange failed");
  });
  const allowed = () =>
    member.evaluate(
      async () => (await (await fetch("/api/access/status")).json()).allowed,
    );
  assert.equal(await allowed(), false);
  await page.goto(owner);
  await page.evaluate(async () => {
    await fetch("/api/bootstrap");
    const csrf = document.cookie
      .split("; ")
      .find((c) => c.startsWith("__Host-owner-csrf="))
      .split("=")[1];
    const res = await fetch("/api/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ idToken: "owner-login" }),
    });
    if (!res.ok) throw Error("owner exchange failed");
  });
  await page.reload();
  await page.locator("#members button").first().click();
  await page.locator("#detail").waitFor({ state: "visible" });
  assert.match(
    await page.locator("#requests").innerText(),
    /<em>literal markup<\/em>/,
  );
  assert.equal(await page.locator("#requests em").count(), 0);
  assert.deepEqual((await accessibility(page).analyze()).violations, []);
  for (const [action, state, access] of [
    ["Approve access", "approved", true],
    ["Revoke access — blocked until reapproved", "revoked", false],
  ]) {
    await page.locator("#action").selectOption({ label: action });
    await page.locator("#verified").check();
    await page.getByRole("button", { name: "Apply decision" }).click();
    await page.waitForFunction(
      (s) => document.querySelector("#state").textContent.startsWith(s),
      state,
    );
    assert.equal(await allowed(), access);
  }
  assert(
    !(await ctx.cookies(reader)).some((c) => c.name.startsWith("__Host-owner")),
  );
  assert(
    !(await ctx.cookies(owner)).some((c) => c.name.startsWith("__Host-family")),
  );
  await page.setViewportSize({ width: 390, height: 844 });
  assert.deepEqual((await accessibility(page).analyze()).violations, []);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.locator("#signin").waitFor({ state: "visible" });
  // Compare the reduced Axe result mode to the full engine on a known-bad page.
  await page.goto("about:blank");
  await page.setContent(
    '<html lang="en"><title>Workshop control</title><main><button></button></main></html>',
  );
  const ids = (result) =>
    result.violations.map((v) => [v.id, v.nodes.map((n) => n.target)]);
  const control = ids(await new AxeBuilder({ page }).analyze());
  assert(control.some(([id]) => id === "button-name"));
  assert.deepEqual(ids(await accessibility(page).analyze()), control);
} finally {
  await browser.close();
}
