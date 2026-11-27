import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { createHash } from "node:crypto";
import { accessibility } from "./browser_checks.mjs";
import { artifacts, routeNames } from "./artifacts.mjs";
// These oracles are flat literal strings. Requiring the checked-in JSON encoding
// also rejects duplicate keys, which JSON.parse would otherwise discard.
export function prototypeExpectations(raw) {
  const value = JSON.parse(raw);
  const fields = [
    "infant",
    "infantDates",
    "later",
    "laterDates",
    "laterToken",
    "elder",
    "grandparent",
    "grandparentToken",
    "recordLabel",
    "hiddenName",
    "hiddenToken",
    "publicToken",
    "publicProse",
  ];
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value).length !== fields.length ||
    !fields.every(
      (key) =>
        Object.hasOwn(value, key) &&
        typeof value[key] === "string" &&
        /^[\p{L}\d][\p{L}\d .,’()–-]{0,159}$/u.test(value[key]),
    ) ||
    raw !== JSON.stringify(value, null, 2) + "\n"
  ) {
    throw new Error("Invalid prototype expectations");
  }
  return value;
}

// When linked host libraries rasterize differently, require the independently
// installed released renderer to produce exactly the same bytes on this host.
export function matchesRender(hash, name, proposed, reference) {
  if (hash === proposed.captures[name]) return true;
  return Boolean(
    reference &&
    proposed.sourceHash === reference.committed.sourceHash &&
    proposed.sourceHash === reference.rendered.sourceHash &&
    proposed.captures[name] === reference.committed.captures[name] &&
    hash === reference.rendered.captures[name],
  );
}

export async function checkPrototype({
  directory,
  specPath,
  verifyRenders,
  sourceHash,
  withBrowser,
  openArtifact,
  expectedPath,
}) {
  const expected = prototypeExpectations(await readFile(expectedPath, "utf8"));
  const manifest = JSON.parse(
    await readFile(directory + "/manifest.json", "utf8"),
  );
  assert.equal(
    manifest.sourceHash,
    await sourceHash(),
    "Prototype changed: regenerate screenshots",
  );
  assert.deepEqual(
    (await readdir(directory)).filter((f) => /\.(png|svg)$/.test(f)).sort(),
    artifacts.map((a) => a.name).sort(),
  );
  const reference = process.env.RELEASED_PROTOTYPE_CAPTURES
    ? JSON.parse(
        await readFile(process.env.RELEASED_PROTOTYPE_CAPTURES, "utf8"),
      )
    : null;
  const spec = await readFile(specPath, "utf8");
  for (const item of artifacts) {
    assert(spec.includes(item.name), `Documentation missing ${item.name}`);
    const bytes = await readFile(`${directory}/${item.name}`);
    assert.equal(bytes.subarray(1, 4).toString(), "PNG");
    assert.equal(
      createHash("sha256").update(bytes).digest("hex"),
      manifest.captures[item.name],
    );
  }
  assert.deepEqual(
    await readFile(directory + "/withheld-url.png"),
    await readFile(directory + "/unknown-url.png"),
  );

  await withBrowser(async ({ context, base }) => {
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    for (const item of artifacts) {
      await openArtifact(page, base, item);
      assert.equal(await page.locator("h1").count(), 1, item.name);
      const loadedFaces = await page.evaluate(() =>
        Array.from(document.fonts)
          .filter((f) => f.status === "loaded")
          .map((f) => f.family),
      );
      assert(
        loadedFaces.includes("Archive Sans") &&
          loadedFaces.includes("Archive Serif"),
        `Bundled fonts not loaded: ${item.name}`,
      );
      if (verifyRenders) {
        const target = item.element ? page.locator(item.element) : page;
        const fresh = await target.screenshot({
          animations: "disabled",
          ...(item.element ? {} : { fullPage: true }),
        });
        const hash = createHash("sha256").update(fresh).digest("hex");
        assert(
          matchesRender(hash, item.name, manifest, reference),
          `Fresh render differs from the accepted capture: ${item.name} (${hash})`,
        );
      }
      // Evidence cards have a consistent shape; ordinary prose never implies hidden evidence.
      for (const fields of await page
        .locator(".claim")
        .evaluateAll((cards) =>
          cards.map((c) => c.querySelectorAll(".dimension").length),
        ))
        assert.equal(fields, 4, item.name);
      assert.deepEqual(
        (await page.locator("html").getAttribute("data-routes")).split(","),
        routeNames,
      );
      assert.equal(
        await page.locator('a[href="#"]').count(),
        0,
        `Dead link: ${item.name}`,
      );
      assert(
        await page
          .getByRole("navigation", { name: "Primary", exact: true })
          .isVisible(),
      );
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `Horizontal overflow: ${item.name}`,
      );
      const result = await accessibility(page)
        .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
        .analyze();
      assert.deepEqual(
        result.violations.map((v) => ({
          id: v.id,
          nodes: v.nodes.map((n) => n.target),
        })),
        [],
        item.name,
      );
    }
    assert.deepEqual(errors, []);
    await page.goto(base + "/?view=evidence");
    for (const [id, kind] of [
      ["birth", "conflict"],
      ["corrected", "corrected"],
      ["identity", "candidate"],
      ["limited", "candidate"],
      ["persona", "accepted"],
    ]) {
      assert.equal(
        await page.locator(`#${id} > .badge.${kind}`).count(),
        1,
        id,
      );
    }
    await page.goto(base + "/?view=search");
    assert.equal(await page.locator(".result").count(), 3);
    for (const [name, dates] of [
      [expected.infant, expected.infantDates],
      [expected.later, expected.laterDates],
    ]) {
      await page.goto(base + "/?view=search");
      await page
        .getByRole("link")
        .filter({ has: page.getByRole("heading", { name, exact: true }) })
        .click();
      await page.getByRole("heading", { name, exact: true }).waitFor();
      assert((await page.locator("main").innerText()).includes(dates));
    }
    await page.goto(base + "/?view=tree&reference=sr");
    await page.locator(".tree a").filter({ hasText: expected.infant }).focus();
    await page.keyboard.press("Enter");
    await page.waitForURL("**/*focus=infant*");
    assert.match(await page.locator("h1").innerText(), /Jr/);
    assert.equal(
      await page.evaluate(() => document.activeElement.tagName),
      "H1",
    );
    assert.match(await page.locator("main").innerText(), /is a child of/);
    assert(!(await page.locator(".notice").innerText()).includes("Sr.."));
    await page.locator(".tree a").filter({ hasText: expected.later }).click();
    assert(
      (await page.locator("h1").innerText()).includes(expected.laterToken),
    );
    await page.goBack();
    assert.match(await page.locator("h1").innerText(), /Jr/);
    await page.goBack();
    assert.match(await page.locator("h1").innerText(), /Sr/);
    await page
      .locator(".tree a")
      .filter({ hasText: expected.grandparent })
      .click();
    assert(
      (await page.locator("h1").innerText()).includes(
        expected.grandparentToken,
      ),
    );
    await page.locator(".tree a").filter({ hasText: expected.elder }).click();
    assert.match(await page.locator("h1").innerText(), /Sr/);

    await page.goto(base + "/?view=tree&focus=measurer&reference=infant");
    assert.match(
      await page.locator(".notice").innerText(),
      /Longer paths are not calculated here/,
    );
    assert(
      !(await page.locator(".notice").innerText()).includes(
        "No relationship path is established",
      ),
    );
    await page.goto(
      base + "/?view=search&query=" + encodeURIComponent(expected.infant),
    );
    assert.equal(await page.locator(".result").count(), 2);
    assert(
      (
        await page
          .locator(".result")
          .filter({ hasText: expected.laterToken })
          .innerText()
      ).includes(expected.recordLabel),
    );

    // Public DOM and metadata must be identical for withheld and nonexistent URLs.
    let absent;
    for (const route of [
      "withheld",
      "unknown",
      "not-a-route",
      "toString",
      "profile&person=apprenticekin",
      "profile&person=nobody",
      "profile&person=toString",
      "tree&focus=apprenticekin",
      "tree&focus=nobody",
      "tree&focus=toString",
      "source&record=account",
      "source&record=nobody",
      "source&record=toString",
    ]) {
      const response = await page.goto(base + `/?view=${route}`);
      assert.equal(response.status(), 404);
      const html = await page.content();
      const normalized = html.replace(/data-routes="[^"]*"/, "");
      if (absent) assert.equal(normalized, absent);
      else absent = normalized;
    }
    for (const route of [
      "profile&person=apprenticekin",
      "tree&focus=apprenticekin",
      "source&record=account",
    ]) {
      const response = await page.goto(
        base + `/?view=${route}&projection=family`,
      );
      assert.equal(response.status(), 200);
      assert(!(await page.locator("h1").innerText()).includes("can’t find"));
    }
    let zero;
    for (const query of [expected.hiddenName, "Nobody Matches"]) {
      await page.goto(
        base + `/?view=search&query=${encodeURIComponent(query)}`,
      );
      const result = await page.locator(".empty").innerHTML();
      if (zero) assert.equal(result, zero);
      else zero = result;
      assert.equal(await page.locator(".result").count(), 0);
    }
    await page.goto(
      base + "/?view=search&query=%3Cimg%20src=x%20onerror=alert(1)%3E",
    );
    assert.equal(await page.locator("main img").count(), 0);
    for (const route of [
      "privacy",
      "tree&focus=carver",
      "sources",
      "source&record=memorial",
    ]) {
      await page.goto(base + `/?view=${route}`);
      const html = await page.content();
      assert(!html.includes(expected.hiddenToken), route);
      assert(!html.includes("record=account"), route);
    }
    await page.goto(base + "/?view=privacy");
    assert.equal(await page.locator(".claim").count(), 0);
    assert.equal(
      await page
        .getByText(expected.publicProse, {
          exact: true,
        })
        .getAttribute("class"),
      "prose",
    );
    await page.goto(base + "/?view=privacy&projection=family");
    assert.match(await page.locator(".claim").innerText(), /Family testimony/);
    await page.getByRole("button", { name: "Simulate session expiry" }).click();
    await page.waitForURL("**/?view=access&state=revoked");
    const expired = await page.content();
    assert(
      !expired.includes(expected.hiddenToken) &&
        !expired.includes(expected.publicToken) &&
        !expired.includes("projection=family"),
    );
    // location.replace removes this family document from the immediate back-stack.
    await page.goBack();
    assert(!/projection=family/.test(page.url()));

    // Percent-encoded sibling-path traversal must never read outside the root.
    for (const path of [
      "/..%2fprototype-extra%2fsecret.txt",
      "/%2e%2e%2fscripts%2fserver.mjs",
      "/missing.css",
    ]) {
      const response = await context.request.get(base + path);
      assert.equal(response.status(), 404);
      assert.equal(await response.text(), "Not found");
    }
    // Numerical focus and muted-text contrast, beyond axe's page checks.
    const lum = (hex) => {
      const c = hex
        .match(/[a-f\d]{2}/gi)
        .map((v) => parseInt(v, 16) / 255)
        .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
      return c[0] * 0.2126 + c[1] * 0.7152 + c[2] * 0.0722;
    };
    const contrast = (a, b) =>
      (Math.max(lum(a), lum(b)) + 0.05) / (Math.min(lum(a), lum(b)) + 0.05);
    for (const bg of ["f7f3eb", "fffdf8", "ffffff"]) {
      assert(contrast("805400", bg) >= 3);
      assert(contrast("56615b", bg) >= 4.5);
    }
  });
  console.log(
    `${artifacts.length} routes/captures checked; axe AA, keyboard navigation, privacy, history, escaping, and server checks passed.${verifyRenders ? " All fresh renders match the accepted pixel oracle." : ""}`,
  );
}
