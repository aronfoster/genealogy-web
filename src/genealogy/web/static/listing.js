/* Bounded same-route loading. History stores navigation metadata, never records. */
(() => {
  const listing = document.querySelector("[data-listing]");
  if (!listing || !window.fetch || !window.AbortController) return;
  const cards = listing.querySelector(".cards");
  const controls = listing.querySelector("[data-listing-controls]");
  const button = listing.querySelector("[data-load-more]");
  const status = listing.querySelector("[data-listing-status]");
  const sentinel = listing.querySelector("[data-listing-sentinel]");
  if (!cards || !controls || !button || !status || !sentinel) return;
  const family = listing.dataset.listingAudience === "family";
  // An older cached session controller cannot authorize progressive loading.
  if (family && !window.familyAccess) return;
  let key = location.pathname + location.search;
  const initialOffset = Number(listing.dataset.listingOffset);
  let displayOffset = initialOffset;
  let token = listing.dataset.listingToken;
  const sources = listing.dataset.listingKind === "sources";
  let total = sources ? Number(listing.dataset.listingTotal) : null;
  let next = listing.dataset.nextUrl;
  let offset = initialOffset;
  let pages = 1;
  let generation = 0;
  let pending = null;
  let pendingManual = false;
  let manualRequested = false;
  let notice = "";
  let ready = !family || window.familyAccess.verified;
  let paused = false;
  let failed = false;
  let restoring = false;
  let initialized = false;
  let starting = false;
  let revoked = false;
  let observer;
  const saved = history.state?.listing;
  history.scrollRestoration = "manual";
  const save = () => {
    if (restoring || !initialized) return;
    try {
      history.replaceState(
        {
          ...history.state,
          listing: {
            key,
            pages: Math.min(pages, 20),
            scrollY: window.scrollY,
            token,
            audience: listing.dataset.listingAudience,
            paused,
          },
        },
        "",
      );
    } catch {
      /* Metadata restoration is optional when history is unavailable. */
    }
  };
  const cancel = () => {
    generation++;
    pending?.abort();
    pending = null;
    pendingManual = false;
    button.disabled = false;
  };
  const lost = () => {
    revoked = true;
    manualRequested = false;
    ready = false;
    cancel();
    cards.replaceChildren();
    document.dispatchEvent(new Event("family-access:invalidate"));
  };
  const draw = (message) => {
    listing.dataset.nextUrl = next;
    listing.dataset.listingToken = token;
    button.hidden = !next;
    button.disabled = !!pending;
    button.textContent = failed ? "Retry loading" : "Load more";
    sentinel.hidden = !next;
    const count = cards.querySelectorAll("article").length;
    const shown =
      count && displayOffset
        ? `${displayOffset + 1}–${displayOffset + count}`
        : count;
    const summary = sources ? `Showing ${shown} of ${total} sources. ` : "";
    status.textContent =
      summary + (message || notice || (!next ? "End of results." : ""));
  };
  const urlFor = (value) => {
    const url = new URL(value, location.href);
    const base = new URL(location.href);
    if (
      url.origin !== base.origin ||
      url.pathname !== base.pathname ||
      url.hash
    )
      throw Error();
    const raw = url.searchParams.get("offset");
    if (!raw || !/^\d{1,6}$/.test(raw)) throw Error();
    const a = new URLSearchParams(url.search),
      b = new URLSearchParams(base.search);
    a.delete("offset");
    b.delete("offset");
    if ((a.get("q") || "").trim() === (b.get("q") || "").trim()) {
      a.delete("q");
      b.delete("q");
    }
    if (sources && (a.get("kind") || "") === (b.get("kind") || "")) {
      a.delete("kind");
      b.delete("kind");
    }
    if (!sources && (a.get("scope") || "tree") === (b.get("scope") || "tree")) {
      a.delete("scope");
      b.delete("scope");
    }
    a.sort();
    b.sort();
    if (a.toString() !== b.toString()) throw Error();
    return url;
  };
  const firstUrl = () => {
    const url = new URL(location.href);
    url.searchParams.set("offset", "0");
    url.hash = "";
    return url.href;
  };
  const resetUrl = () => {
    const url = new URL(location.href);
    url.searchParams.delete("offset");
    url.hash = "";
    try {
      history.replaceState(history.state, "", url.href);
      key = url.pathname + url.search;
    } catch {
      /* The first page remains usable when history is unavailable. */
    }
  };
  const read = async (url, controller) => {
    const response = await fetch(urlFor(url), {
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
      redirect: "error",
    });
    if (
      !response.ok ||
      !response.headers.get("content-type")?.includes("text/html")
    )
      throw Error();
    const doc = new DOMParser().parseFromString(
      await response.text(),
      "text/html",
    );
    const result = doc.querySelector("[data-listing]");
    if (!result || result.dataset.listingKind !== listing.dataset.listingKind)
      throw Error();
    const audience = result.dataset.listingAudience;
    if (family && audience !== "family") {
      lost();
      throw Error();
    }
    if (audience !== listing.dataset.listingAudience) {
      if (!family && audience === "family") {
        ready = false;
        cancel();
        // A new document installs the family session controller before loading.
        window.location.reload();
      }
      throw Error();
    }
    const resultToken = result.dataset.listingToken;
    if (
      !sources &&
      (result.dataset.peopleScope !== listing.dataset.peopleScope ||
        result.dataset.listingQuery !== listing.dataset.listingQuery)
    )
      throw Error();
    if (!new RegExp("^" + audience + ":[0-9a-f]{64}$").test(resultToken || ""))
      throw Error();
    const resultTotal = sources ? Number(result.dataset.listingTotal) : null;
    if (
      sources &&
      (result.dataset.listingQuery !== listing.dataset.listingQuery ||
        result.dataset.sourceKind !== listing.dataset.sourceKind ||
        !Number.isSafeInteger(resultTotal) ||
        resultTotal < 0)
    )
      throw Error();
    const rows = [...result.querySelectorAll(".cards > article")];
    const resultOffset = Number(result.dataset.listingOffset);
    const expectedOffset = Number(urlFor(url).searchParams.get("offset"));
    if (rows.length > 25 || resultOffset !== expectedOffset) throw Error();
    const following = result.dataset.nextUrl;
    if (
      following &&
      Number(urlFor(following).searchParams.get("offset")) !== resultOffset + 25
    )
      throw Error();
    if (sources && resultToken === token && resultTotal !== total)
      throw Error();
    return {
      rows,
      token: resultToken,
      next: following,
      offset: resultOffset,
      total: resultTotal,
      filters: result.querySelector("[data-source-filters]"),
      empty: result.querySelector("[data-source-empty]"),
    };
  };
  const rowKey = (row) => row.querySelector("h2 a, h3 a")?.getAttribute("href");
  const load = async (manual = false) => {
    if (revoked) return false;
    if (manual && family && (!ready || !initialized || restoring)) {
      manualRequested = true;
      if (!ready) draw("Verifying family access…");
      return false;
    }
    if (!ready || pending || !next || (failed && !manual)) return false;
    if (manual) manualRequested = false;
    failed = false;
    notice = "";
    const current = generation;
    const controller = new AbortController();
    pending = controller;
    pendingManual = manual;
    const timeout = setTimeout(() => controller.abort(), 10000);
    draw("Loading more " + listing.dataset.listingKind + "…");
    try {
      let result = await read(next, controller);
      if (current !== generation || !ready) return false;
      if (result.offset !== offset + 25) throw Error();
      if (result.token !== token) {
        // Never append a new ordering at an old offset. Fetch a bounded first page.
        listing.classList.add("listing-restoring");
        result = await read(firstUrl(), controller);
        if (current !== generation || !ready) return false;
        cards.replaceChildren(
          ...result.rows.map((row) => document.importNode(row, true)),
        );
        token = result.token;
        displayOffset = 0;
        if (sources) {
          total = result.total;
          listing.dataset.listingTotal = String(total);
          // Counts refresh from the applied query; preserve edits not yet submitted.
          const oldForm = listing.querySelector("[data-source-filters]");
          const focused = oldForm?.contains(document.activeElement)
            ? document.activeElement
            : null;
          const selection =
            focused?.id === "q"
              ? [focused.selectionStart, focused.selectionEnd]
              : null;
          for (const id of ["q", "kind"]) {
            const oldControl = oldForm?.querySelector(`#${id}`);
            const newControl = result.filters?.querySelector(`#${id}`);
            if (!oldControl || !newControl) continue;
            if (
              id === "kind" &&
              ![...newControl.options].some(
                (option) => option.value === oldControl.value,
              )
            ) {
              const option = oldControl.selectedOptions[0].cloneNode(true);
              option.textContent = option.textContent.replace(
                /\(\d+\)$/,
                "(0)",
              );
              newControl.append(option);
            }
            newControl.value = oldControl.value;
          }
          for (const [selector, node] of [
            ["[data-source-filters]", result.filters],
            ["[data-source-empty]", result.empty],
          ]) {
            if (node)
              listing
                .querySelector(selector)
                ?.replaceWith(document.importNode(node, true));
          }
          // Apply to the imported controls: cloning a select can restore its
          // original selected attribute instead of its unsubmitted selection.
          for (const id of ["q", "kind"]) {
            const oldControl = oldForm?.querySelector(`#${id}`);
            const control = listing.querySelector(
              `[data-source-filters] #${id}`,
            );
            if (oldControl && control) control.value = oldControl.value;
          }
          if (focused) {
            const control = listing.querySelector(
              `[data-source-filters] #${focused.id}`,
            );
            control?.focus({ preventScroll: true });
            if (selection) control?.setSelectionRange(...selection);
          }
        }
        next = result.next;
        offset = 0;
        pages = 1;
        paused = true;
        notice =
          "The archive changed. Showing the first page; use Load more to continue.";
        resetUrl();
        window.scrollTo(0, 0);
        if (restoring) return false;
        draw();
        save();
        return false;
      }
      const seen = new Set([...cards.children].map(rowKey));
      const added = [];
      for (const row of result.rows) {
        const id = rowKey(row);
        if (!id) throw Error();
        if (!seen.has(id)) {
          seen.add(id);
          added.push(document.importNode(row, true));
        }
      }
      cards.append(...added);
      next = result.next;
      offset = result.offset;
      pages++;
      notice = `${added.length} more ${listing.dataset.listingKind} loaded.${next ? "" : " End of results."}`;
      draw();
      if (manual) {
        paused = false;
        added[0]?.querySelector("h2 a, h3 a")?.focus({ preventScroll: true });
      }
      save();
      return true;
    } catch {
      if (current === generation && ready) {
        failed = true;
        notice = "Could not load more results. Use Retry loading to try again.";
        draw();
      }
      return false;
    } finally {
      clearTimeout(timeout);
      if (current === generation) {
        pending = null;
        pendingManual = false;
        button.disabled = false;
        if (!restoring) listing.classList.remove("listing-restoring");
      }
    }
  };
  const resumeManual = () => {
    if (manualRequested && initialized && ready && !restoring && !pending)
      void load(true);
  };
  const start = async () => {
    if (initialized || starting || !ready || revoked) return;
    starting = true;
    const current = generation;
    try {
      paused = saved?.key === key && saved.paused === true;
      const count = saved?.pages;
      const firstRows = [...cards.children].map((row) => row.cloneNode(true));
      const firstNext = next;
      const firstToken = token;
      if (
        saved?.key === key &&
        saved.token === token &&
        saved.audience === listing.dataset.listingAudience &&
        Number.isInteger(count) &&
        count > 1 &&
        count <= 20 &&
        initialOffset === 0
      ) {
        restoring = true;
        listing.classList.add("listing-restoring");
        let complete = true;
        for (let i = 1; i < count; i++) {
          if (!(await load())) {
            complete = false;
            break;
          }
        }
        if (!complete && current !== generation) {
          // Focus/pageshow can revalidate twice while a Back batch is in flight.
          // Preserve the original metadata and replay only after verification.
          if (!revoked) {
            cards.replaceChildren(...firstRows);
            token = firstToken;
            next = firstNext;
            offset = initialOffset;
            pages = 1;
          }
          restoring = false;
          return;
        }
        if (!complete && ready && token === firstToken) {
          cards.replaceChildren(...firstRows);
          next = firstNext;
          offset = initialOffset;
          pages = 1;
          window.scrollTo(0, 0);
        }
        restoring = false;
        if (complete && ready && Number.isFinite(saved.scrollY))
          window.scrollTo(0, saved.scrollY);
      }
      if (revoked || !ready) return;
      initialized = true;
      listing.classList.remove("listing-restoring");
      if (
        count === 1 &&
        saved?.key === key &&
        saved.token === token &&
        saved.audience === listing.dataset.listingAudience &&
        Number.isFinite(saved.scrollY)
      )
        window.scrollTo(0, saved.scrollY);
      if (saved?.key === key && saved.token !== token) window.scrollTo(0, 0);
      draw();
      save();
      if ("IntersectionObserver" in window) {
        observer = new IntersectionObserver(
          (entries) => {
            if (
              entries.some((entry) => entry.isIntersecting) &&
              !paused &&
              !restoring
            )
              void load();
          },
          { rootMargin: "600px" },
        );
        observer.observe(sentinel);
      }
    } finally {
      starting = false;
      // A fast status response may have verified access before the abort settled.
      if (!initialized && ready && !revoked) void start();
      else if (!revoked) resumeManual();
    }
  };
  const resume = () => {
    if (revoked) return;
    ready = true;
    draw();
    void start();
    resumeManual();
    if (observer) {
      observer.disconnect();
      observer.observe(sentinel);
    }
  };
  controls.hidden = false;
  listing.querySelector(".pagination")?.setAttribute("hidden", "");
  if (saved?.key === key && saved.pages > 1)
    listing.classList.add("listing-restoring");
  button.addEventListener("click", () => void load(true));
  listing.addEventListener("submit", () => {
    save();
    cancel();
  });
  listing.addEventListener("click", (event) => {
    if (event.target.closest?.("[data-clear-filters]")) {
      save();
      cancel();
    }
  });
  let scrollTimer;
  window.addEventListener(
    "scroll",
    () => {
      if (!scrollTimer)
        scrollTimer = setTimeout(() => {
          scrollTimer = null;
          save();
        }, 150);
    },
    { passive: true },
  );
  window.addEventListener("pagehide", () => {
    save();
    manualRequested = false;
    ready = false;
    cancel();
    draw();
  });
  window.addEventListener("pageshow", () => {
    if (!family) {
      resume();
    }
  });
  if (family) {
    document.addEventListener("family-access:paused", () => {
      manualRequested ||= pendingManual;
      ready = false;
      cancel();
      draw("Verifying family access…");
    });
    document.addEventListener("family-access:lost", () => {
      revoked = true;
      manualRequested = false;
      ready = false;
      cancel();
      cards.replaceChildren();
    });
    document.addEventListener("family-access:verified", () => {
      resume();
    });
  }
  draw();
  void start();
})();
