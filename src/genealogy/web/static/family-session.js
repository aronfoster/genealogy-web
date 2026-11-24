/* No genealogy payloads or credentials are stored. Recheck on every resumption. */
(() => {
  const access = { verified: false };
  window.familyAccess = access;
  const accountPage = !!document.querySelector(
    ".access-panel[data-access-state]",
  );
  const signal = (name) =>
    document.dispatchEvent(new Event("family-access:" + name));
  let pending = false;
  let timer;
  let generation = 0;
  const pause = () => {
    generation++;
    pending = false;
    conceal();
    clearInterval(timer);
  };
  const conceal = () => {
    access.verified = false;
    document.documentElement.classList.add("family-revalidating");
    signal("paused");
  };
  const leave = (publicOnly = false) => {
    conceal();
    signal("lost");
    if (accountPage) window.location.reload();
    else if (
      publicOnly &&
      document.body.hasAttribute("data-public-alternative")
    ) {
      const destination = new URL(window.location.href);
      destination.searchParams.set("public", "1");
      window.location.replace(destination.href);
    } else window.location.replace("/access");
  };
  const validate = async () => {
    if (pending || document.hidden) return;
    pending = true;
    const current = generation;
    conceal();
    try {
      const response = await fetch("/api/access/status", {
        credentials: "same-origin",
        cache: "no-store",
        signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) throw new Error("Access status unavailable");
      const { allowed } = await response.json();
      if (typeof allowed !== "boolean")
        throw new Error("Invalid access status");
      if (current !== generation || document.hidden) return;
      if (!allowed) return leave();
      access.verified = true;
      signal("verified");
      document.documentElement.classList.remove("family-revalidating");
    } catch {
      if (current === generation && !document.hidden) {
        // Account controls contain no archive payload. Keep Sign out usable on
        // transient failures; a confirmed denial above still refreshes the page.
        if (accountPage)
          document.documentElement.classList.remove("family-revalidating");
        else leave(true);
      }
    } finally {
      if (current === generation) pending = false;
    }
  };
  const resume = () => {
    clearInterval(timer);
    if (!document.hidden) {
      void validate();
      timer = setInterval(validate, 60000);
    } else pause();
  };
  window.addEventListener("pagehide", pause);
  window.addEventListener("pageshow", resume);
  window.addEventListener("focus", resume);
  document.addEventListener("visibilitychange", resume);
  if ("BroadcastChannel" in window) {
    new BroadcastChannel("family-access").addEventListener("message", resume);
  }
  window.addEventListener("storage", (event) => {
    if (event.key === "family-access-signal") resume();
  });
  document.addEventListener("family-access:invalidate", () => leave());
  resume();
})();
