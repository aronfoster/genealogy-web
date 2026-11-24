import { manualListingOnly } from "./check_navigation.mjs";
import assert from "node:assert/strict";

export async function checkListings(context, page, base, family) {
  const ready = async () => {
    await page.waitForFunction(() => {
      const list = document.querySelector("[data-listing]");
      return (
        list &&
        !list.querySelector("[data-listing-controls]").hidden &&
        !list.classList.contains("listing-restoring") &&
        !document.documentElement.classList.contains("family-revalidating")
      );
    });
  };
  const waitForRequest = async (promise) => {
    let timer;
    try {
      await Promise.race([
        promise,
        new Promise((_, reject) => {
          timer = setTimeout(
            () => reject(Error("Delayed request did not start")),
            20000,
          );
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  };
  const count = () => page.locator("[data-listing] .cards > article").count();
  const statusIncludes = (text) =>
    page.waitForFunction(
      (text) =>
        document
          .querySelector("[data-listing-status]")
          ?.textContent.includes(text) &&
        !document.documentElement.classList.contains("family-revalidating") &&
        window.familyAccess?.verified !== false,
      text,
    );
  const pause = async () => {
    await manualListingOnly(page);
  };
  const menuAvailable = async () => {
    const toggle = page.locator("[data-site-menu] summary");
    const box = await toggle.boundingBox();
    assert(
      box && box.y >= 0 && box.y + box.height <= page.viewportSize().height,
    );
    await toggle.click();
    assert(
      await page
        .getByRole("navigation", { name: "Main", exact: true })
        .getByRole("link", { name: "Sources", exact: true })
        .isVisible(),
    );
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("[data-listing-footer]").count(), 0);
  };
  const goto = async (path) => {
    // A same-URL navigation may restore its existing history entry. Start each
    // independent scenario from a fresh document; Back checks use goBack directly.
    await page.goto("about:blank");
    await page.goto(base + path);
    await ready();
  };
  // Compare browser order and completeness with bounded API inventories.
  for (const [path, kind, width] of [
    ["/people", "people", 1280],
    ["/sources", "sources", 390],
  ]) {
    await page.setViewportSize({ width, height: 844 });
    const expected = [];
    const expectedIndicators = [];
    for (let offset = 0; ; offset += 25) {
      const data = await (
        await context.request.get(base + `/api${path}?offset=${offset}`)
      ).json();
      assert(data[kind].length <= 25);
      expected.push(...data[kind].map((row) => row.id));
      if (kind === "people")
        expectedIndicators.push(
          ...data.people.map((row) => ({
            membership: row.membership?.icon ? row.membership.label : "",
            attributes: row.attributes.map((a) => a.label),
          })),
        );
      if (data.next === null) break;
    }
    await goto(path);
    assert.equal(await count(), Math.min(25, expected.length));
    // Repeated observer crossings cannot issue simultaneous duplicate requests.
    const offsets = [];
    const listener = (request) => {
      const url = new URL(request.url());
      if (request.resourceType() === "fetch" && url.pathname === path)
        offsets.push(url.searchParams.get("offset"));
    };
    page.on("request", listener);
    for (let target = 50; (await count()) < expected.length; target += 25) {
      await page.locator("[data-listing-sentinel]").scrollIntoViewIfNeeded();
      await page.waitForFunction(
        (n) =>
          document.querySelectorAll("[data-listing] .cards > article").length >=
          n,
        Math.min(target, expected.length),
      );
    }
    page.off("request", listener);
    assert.equal(new Set(offsets).size, offsets.length);
    const links = await page
      .locator("[data-listing] .cards > article")
      .evaluateAll((rows) =>
        rows.map((row) =>
          row
            .querySelector("h2 a, h3 a")
            .getAttribute("href")
            .split("/")
            .at(-1),
        ),
      );
    assert.deepEqual(links, expected);
    if (kind === "people") {
      const indicators = await page
        .locator("[data-listing] .cards > article")
        .evaluateAll((rows) =>
          rows.map((row) => ({
            membership:
              row
                .querySelector(".person-membership summary")
                ?.getAttribute("aria-label") ||
              row.querySelector(".person-membership > span:last-child")
                ?.textContent ||
              "",
            attributes: [
              ...row.querySelectorAll(".person-attributes summary"),
            ].map((a) => a.getAttribute("aria-label")),
          })),
        );
      assert.deepEqual(
        indicators,
        expectedIndicators,
        "Appended cards preserve server evidence-backed indicators",
      );
    }
    assert.equal(new Set(links).size, expected.length);
    console.log(`PASS: ${kind} scrolling (${expected.length} cards)`);
    await statusIncludes("End of results");
    await menuAvailable();
    if (kind === "people")
      assert.equal(
        await page
          .locator(".cards .eyebrow")
          .filter({ hasText: /^Person$/ })
          .count(),
        0,
      );
    assert(await page.locator("[data-load-more]").isHidden());
  }
  // Manual loading focuses a new card; automatic loading can continue afterward.
  await goto("/people");
  await pause();
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(
    () =>
      document.querySelectorAll("[data-listing] .cards > article").length ===
      50,
  );
  await page.locator("[data-listing-sentinel]").evaluate((node) => {
    node.style.removeProperty("display");
  });
  await page.locator("[data-listing-sentinel]").scrollIntoViewIfNeeded();
  await page.waitForFunction(
    () =>
      document.querySelectorAll("[data-listing] .cards > article").length >= 75,
  );
  console.log("PASS: manual and automatic loading");
  await page.emulateMedia({ media: "print" });
  assert.equal(await page.locator("[data-listing-footer]").count(), 0);
  assert(await page.locator(".site-header").isHidden());
  assert(await page.locator("[data-listing-controls]").isHidden());
  assert(await page.locator("[data-listing-status]").isHidden());
  await page.emulateMedia({ media: "screen" });
  if (family) {
    // A click arriving during focus revalidation waits for current access.
    await goto("/people");
    await pause();
    let statusStarted, statusRelease;
    const statusStart = new Promise((resolve) => {
      statusStarted = resolve;
    });
    const statusGate = new Promise((resolve) => {
      statusRelease = resolve;
    });
    await page.route("**/api/access/status", async (route) => {
      const response = await route.fetch();
      statusStarted();
      await statusGate;
      await route.fulfill({ response }).catch(() => {});
    });
    await page.evaluate(() => {
      window.dispatchEvent(new Event("focus"));
      document.querySelector("[data-load-more]").click();
    });
    await waitForRequest(statusStart);
    assert.equal(await count(), 25);
    assert.doesNotMatch(
      await page.locator("[data-listing-status]").textContent(),
      /Loading more/,
    );
    statusRelease();
    await ready();
    await page.waitForFunction(
      () =>
        document.querySelectorAll("[data-listing] .cards > article").length ===
        50,
    );
    assert.doesNotMatch(
      await page.locator("[data-listing-status]").textContent(),
      /Loading more/,
    );
    await page.unroute("**/api/access/status");
    // A revalidation also retries an interrupted manual request, discarding its late response.
    await goto("/people");
    await pause();
    let manualStarted,
      manualRelease,
      manualRequests = 0;
    const manualStart = new Promise((resolve) => {
      manualStarted = resolve;
    });
    const manualGate = new Promise((resolve) => {
      manualRelease = resolve;
    });
    await page.route("**/people?*offset=25*", async (route) => {
      if (++manualRequests !== 1) return route.continue();
      const response = await route.fetch();
      manualStarted();
      await manualGate;
      await route.fulfill({ response }).catch(() => {});
    });
    await page.locator("[data-load-more]").click();
    await waitForRequest(manualStart);
    const pausedStatus = await page.evaluate(() => {
      window.dispatchEvent(new Event("focus"));
      return document.querySelector("[data-listing-status]").textContent;
    });
    assert.doesNotMatch(pausedStatus, /Loading more/);
    manualRelease();
    await ready();
    await page.waitForFunction(
      () =>
        document.querySelectorAll("[data-listing] .cards > article").length ===
        50,
    );
    assert.equal(manualRequests, 2);
    assert.doesNotMatch(
      await page.locator("[data-listing-status]").textContent(),
      /Loading more/,
    );
    await page.unroute("**/people?*offset=25*");
    console.log("PASS: manual requests survive family revalidation");
    // Pending manual intent must never run if the access check revokes access.
    await goto("/people");
    await pause();
    let revokedStarted, revokedRelease;
    const revokedStart = new Promise((resolve) => {
      revokedStarted = resolve;
    });
    const revokedGate = new Promise((resolve) => {
      revokedRelease = resolve;
    });
    await page.route("**/api/access/status", async (route) => {
      revokedStarted();
      await revokedGate;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: '{"allowed":false}',
      });
    });
    let revokedRequests = 0;
    const revokedListener = (request) => {
      const url = new URL(request.url());
      if (
        request.resourceType() === "fetch" &&
        url.pathname === "/people" &&
        url.searchParams.get("offset") === "25"
      )
        revokedRequests++;
    };
    page.on("request", revokedListener);
    await page.evaluate(() => {
      window.dispatchEvent(new Event("focus"));
      document.querySelector("[data-load-more]").click();
    });
    await waitForRequest(revokedStart);
    revokedRelease();
    await page.waitForURL(base + "/access");
    assert.equal(revokedRequests, 0);
    assert.equal(await page.locator("[data-listing]").count(), 0);
    page.off("request", revokedListener);
    await page.unroute("**/api/access/status");
    console.log("PASS: revoked access discards queued manual requests");
  }
  // A failed automatic batch stops retries until the reader requests one.
  await goto("/people");
  await pause();
  let requests = 0;
  await page.route("**/people?*offset=25*", async (route) => {
    requests++;
    if (requests === 1)
      await route.fulfill({
        status: 503,
        contentType: "text/html",
        body: "unavailable",
      });
    else await route.continue();
  });
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(() =>
    document.querySelector("[data-load-more]").textContent.includes("Retry"),
  );
  assert.equal(requests, 1);
  await menuAvailable();
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(
    () =>
      document.querySelectorAll("[data-listing] .cards > article").length ===
      50,
  );
  assert.equal(requests, 2);
  assert.equal(
    await page
      .locator("[data-listing] .cards > article")
      .nth(25)
      .locator("h2 a, h3 a")
      .evaluate((a) => a === document.activeElement),
    true,
  );
  await page.unroute("**/people?*offset=25*");
  console.log("PASS: listing retry");
  // Back works even when no-store prevents BFCache. Force an additional family
  // revalidation during its first refetch to exercise pause/resume deterministically.
  let backRequests = 0;
  if (family)
    await page.route("**/people?*offset=25*", async (route) => {
      const response = await route.fetch();
      if (++backRequests === 1)
        await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      await route.fulfill({ response }).catch(() => {});
    });
  const backProfile = page
    .locator("[data-listing] .cards > article")
    .nth(30)
    .locator("h3 a");
  const backProfileUrl = base + (await backProfile.getAttribute("href"));
  await backProfile.click();
  await page.waitForURL(backProfileUrl);
  await page.goBack();
  await ready();
  await page.waitForFunction(
    () =>
      document.querySelectorAll("[data-listing] .cards > article").length ===
      50,
  );
  if (family) {
    if (backRequests > 0)
      assert(
        backRequests >= 2,
        "Interrupted restoration must refetch after verification",
      );
    await page.unroute("**/people?*offset=25*");
  }
  const state = await page.evaluate(() => history.state.listing);
  assert.equal(state.pages, 2);
  assert.deepEqual(Object.keys(state).sort(), [
    "audience",
    "key",
    "pages",
    "paused",
    "scrollY",
    "token",
  ]);
  assert((await page.evaluate(() => window.scrollY)) > 0);
  console.log("PASS: listing Back");
  // A stale Back token must leave the first page visible and avoid old offsets.
  await page.route(base + "/people", async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replace(
      /data-listing-token="[^"]+"/,
      `data-listing-token="${family ? "family" : "public"}:${"b".repeat(64)}"`,
    );
    await route.fulfill({ response, body });
  });
  await page.reload();
  await ready();
  assert.equal(await count(), 25);
  assert.equal(await page.evaluate(() => window.scrollY), 0);
  await page.unroute(base + "/people");
  // Search replaces the old listing and returns its own empty/end state.
  await page.locator("#q").fill("no-such-person-zzzz");
  await page.locator("button[type=submit]").click();
  await page.waitForURL(base + "/people?q=no-such-person-zzzz&scope=tree");
  await ready();
  assert.equal(await count(), 0);
  if (family) {
    const checking = await page.evaluate(() => {
      window.dispatchEvent(new Event("focus"));
      return document.querySelector("[data-listing-status]").textContent;
    });
    assert.equal(checking, "Verifying family access…");
  }
  // Revalidation is a legitimate transient state; require the verified final
  // end state rather than sampling between separate asynchronous DOM reads.
  await statusIncludes("End of results");
  console.log("PASS: listing search");
  // Different publication tokens never append into the existing ordering.
  await goto("/people");
  await pause();
  await page.route("**/people?*offset=*", async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replace(
      /data-listing-token="[^"]+"/,
      `data-listing-token="${family ? "family" : "public"}:${"a".repeat(64)}"`,
    );
    await route.fulfill({ response, body });
  });
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(() =>
    document
      .querySelector("[data-listing-status]")
      .textContent.includes("archive changed"),
  );
  assert.equal(await count(), 25);
  // A publication restart pauses real automatic loading until a manual load.
  // Keep the new token on subsequent responses until the scenario is complete.
  let pausedRequests = 0;
  const pausedRequest = (request) => {
    if (new URL(request.url()).searchParams.has("offset")) pausedRequests++;
  };
  page.on("request", pausedRequest);
  await page.locator("[data-listing-sentinel]").evaluate((node) => {
    node.style.removeProperty("display");
  });
  await page.locator("[data-listing-sentinel]").scrollIntoViewIfNeeded();
  await page.waitForTimeout(1000);
  assert.equal(pausedRequests, 0);
  assert.equal(await count(), 25);
  page.off("request", pausedRequest);
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(
    () =>
      document.querySelectorAll("[data-listing] .cards > article").length ===
      50,
  );
  await page.locator("[data-listing-sentinel]").scrollIntoViewIfNeeded();
  await page.waitForFunction(
    () =>
      document.querySelectorAll("[data-listing] .cards > article").length >= 75,
  );
  await page.unroute("**/people?*offset=*");
  console.log("PASS: listing generation restart");
  // A publication changing between the Back document and its replayed batch
  // restarts at the top with a durable notice rather than a stale loading message.
  await goto("/people");
  await pause();
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(
    () =>
      document.querySelectorAll("[data-listing] .cards > article").length ===
      50,
  );
  await pause();
  const changedProfile = page
    .locator("[data-listing] .cards > article")
    .nth(30)
    .locator("h3 a");
  const changedProfileUrl = base + (await changedProfile.getAttribute("href"));
  await changedProfile.click();
  await page.waitForURL(changedProfileUrl);
  let changedBackRequests = 0;
  await page.route("**/people?*offset=25*", async (route) => {
    changedBackRequests++;
    const response = await route.fetch();
    const body = (await response.text()).replace(
      /data-listing-token="[^"]+"/,
      `data-listing-token="${family ? "family" : "public"}:${"c".repeat(64)}"`,
    );
    await route.fulfill({ response, body });
  });
  await page.goBack();
  await ready();
  // If BFCache retained the DOM, force the cold replay path using the same
  // Back entry's saved navigation metadata.
  if (changedBackRequests === 0) {
    await page.reload();
    await ready();
  }
  assert(changedBackRequests > 0);
  assert.equal(await count(), 25);
  assert.equal(await page.evaluate(() => window.scrollY), 0);
  assert.match(
    await page.locator("[data-listing-status]").textContent(),
    /archive changed/,
  );
  await page.unroute("**/people?*offset=25*");
  console.log("PASS: publication change during Back replay");
  // Restarting an explicit offset also normalizes the URL and history key.
  await goto("/people?offset=50");
  await pause();
  await page.route("**/people?*offset=*", async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replace(
      /data-listing-token="[^"]+"/,
      `data-listing-token="${family ? "family" : "public"}:${"d".repeat(64)}"`,
    );
    await route.fulfill({ response, body });
  });
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(() =>
    document
      .querySelector("[data-listing-status]")
      .textContent.includes("archive changed"),
  );
  assert.equal(await count(), 25);
  assert.equal(new URL(page.url()).searchParams.get("offset"), null);
  assert.equal(await page.evaluate(() => history.state.listing.key), "/people");
  await page.unroute("**/people?*offset=*");
  await page.reload();
  await ready();
  assert.equal(
    await page.locator("[data-listing]").getAttribute("data-listing-offset"),
    "0",
  );
  console.log("PASS: explicit-offset publication restart");
  // Discard a late request after pagehide/search navigation.
  await goto("/people");
  await pause();
  let release;
  let started;
  const start = new Promise((resolve) => {
    started = resolve;
  });
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  await page.route("**/people?*offset=25*", async (route) => {
    const response = await route.fetch();
    started();
    await gate;
    await route.fulfill({ response }).catch(() => {});
  });
  await page.locator("[data-load-more]").click();
  await waitForRequest(start);
  await page.goto(base + "/people?q=no-such-person-zzzz");
  release();
  await ready();
  assert.equal(await count(), 0);
  await page.unroute("**/people?*offset=25*");
  console.log("PASS: listing navigation cancellation");
  // Timeout exposes Retry; retry uses a fresh request. Shorten only the loader
  // deadline for this check, while keeping a genuinely stalled network request.
  await goto("/people");
  await pause();
  await page.evaluate(() => {
    window.listingTestTimeout = window.setTimeout;
    window.setTimeout = (callback, delay, ...args) =>
      window.listingTestTimeout(
        callback,
        delay === 10000 ? 100 : delay,
        ...args,
      );
  });
  let timeoutRequests = 0;
  await page.route("**/people?*offset=25*", async (route) => {
    timeoutRequests++;
    if (timeoutRequests === 1) {
      await new Promise((resolve) => setTimeout(resolve, 300));
      await route.abort().catch(() => {});
    } else await route.continue();
  });
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(() =>
    document.querySelector("[data-load-more]").textContent.includes("Retry"),
  );
  await page.evaluate(() => {
    window.setTimeout = window.listingTestTimeout;
    delete window.listingTestTimeout;
  });
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(
    () =>
      document.querySelectorAll("[data-listing] .cards > article").length ===
      50,
  );
  assert.equal(timeoutRequests, 2);
  await page.unroute("**/people?*offset=25*");
  console.log("PASS: listing timeout recovery");
  // Resuming the same in-memory document rejects responses from before pause.
  await goto("/people");
  await pause();
  let resumeRelease, resumeStarted;
  const resumeStart = new Promise((resolve) => {
    resumeStarted = resolve;
  });
  const resumeGate = new Promise((resolve) => {
    resumeRelease = resolve;
  });
  await page.route("**/people?*offset=25*", async (route) => {
    const response = await route.fetch();
    resumeStarted();
    await resumeGate;
    await route.fulfill({ response }).catch(() => {});
  });
  await page.locator("[data-load-more]").click();
  await waitForRequest(resumeStart);
  await menuAvailable();
  await page.evaluate(() => {
    window.dispatchEvent(
      new PageTransitionEvent("pagehide", { persisted: true }),
    );
    window.dispatchEvent(
      new PageTransitionEvent("pageshow", { persisted: true }),
    );
  });
  resumeRelease();
  await ready();
  assert.equal(await count(), 25);
  await page.unroute("**/people?*offset=25*");
  await page.locator("[data-load-more]").click();
  await page.waitForFunction(
    () =>
      document.querySelectorAll("[data-listing] .cards > article").length ===
      50,
  );
  console.log("PASS: listing in-memory resumption");
  if (family) {
    // A silent family-to-public downgrade must leave, never replace/append.
    await goto("/people");
    await pause();
    await page.route("**/people?*offset=25*", async (route) => {
      const response = await route.fetch();
      const body = (await response.text()).replace(
        'data-listing-audience="family"',
        'data-listing-audience="public"',
      );
      await route.fulfill({ response, body });
    });
    await page.locator("[data-load-more]").click();
    await page.waitForURL(base + "/access");
    assert.equal(await page.locator("[data-listing]").count(), 0);
    await page.unroute("**/people?*offset=25*");
  }
  // Disable document scripts on a separate page. A second context is unstable
  // in the single-process packaged Chromium used by the Linux CI harness.
  const fallback = await context.newPage();
  const nojs = await context.newCDPSession(fallback);
  await nojs.send("Emulation.setScriptExecutionDisabled", { value: true });
  await fallback.goto(base + "/people");
  assert.equal(await fallback.locator(".cards > article").count(), 25);
  await fallback.getByRole("link", { name: "Next page", exact: true }).click();
  await fallback.waitForLoadState("domcontentloaded");
  assert.equal(new URL(fallback.url()).searchParams.get("offset"), "25");
  assert.equal(await fallback.locator(".cards > article").count(), 25);
  await fallback.goto(base + "/sources");
  assert.equal(await fallback.locator(".cards > article").count(), 25);
  await fallback.getByRole("link", { name: "Next page", exact: true }).click();
  await fallback.waitForLoadState("domcontentloaded");
  assert.equal(new URL(fallback.url()).searchParams.get("offset"), "25");
  assert.equal(await fallback.locator(".cards > article").count(), 25);
  await fallback.close();
  console.log(
    "PASS: infinite listings, bounded inventories, retry, search, Back, generation, late responses, no-JS and audience checks",
  );
}
