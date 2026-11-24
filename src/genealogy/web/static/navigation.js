/* Native disclosure works without scripts; enhance dismissal and focus only. */
(() => {
  // A failed private-document check uses a one-navigation public opt-out.
  // Remove it without reloading: refresh and later navigation recheck auth,
  // while this already-public document never polls or loops during an outage.
  if (document.body.hasAttribute("data-public-fallback")) {
    const location = new URL(window.location.href);
    location.searchParams.delete("public");
    window.history.replaceState(null, "", location.href);
  }
  const menu = document.querySelector("[data-site-menu]");
  const toggle = menu?.querySelector("summary");
  if (!menu || !toggle) return;
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (menu.open) {
        menu.open = false;
        toggle.focus({ preventScroll: true });
      }
      menu.setAttribute("data-tooltip-dismissed", "");
    }
  });
  // A fresh hover/focus interaction can show the explanation again.
  toggle.addEventListener("pointerenter", () =>
    menu.removeAttribute("data-tooltip-dismissed"),
  );
  toggle.addEventListener("focus", () =>
    menu.removeAttribute("data-tooltip-dismissed"),
  );
  document.addEventListener("click", (event) => {
    if (!menu.contains(event.target)) menu.open = false;
  });
  document.addEventListener("focusin", (event) => {
    if (!menu.contains(event.target)) menu.open = false;
  });
  window.addEventListener("pagehide", () => {
    menu.open = false;
  });
})();

/* Delegated handlers also cover cards appended by infinite scrolling. */
(() => {
  const disclosures = () => document.querySelectorAll(".indicator-explanation");
  const closeOthers = (keep) => {
    for (const detail of disclosures()) {
      if (detail !== keep) detail.open = false;
    }
  };
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const detail = event.target.closest?.(".indicator-explanation");
    closeOthers(null);
    for (const item of disclosures()) item.classList.add("indicator-dismissed");
    if (detail) {
      detail.querySelector("summary").focus({ preventScroll: true });
    }
  });
  document.addEventListener("click", (event) => {
    const detail = event.target.closest?.(".indicator-explanation");
    closeOthers(detail);
    if (detail) {
      const closing = detail.open && event.target.closest("summary");
      detail.classList.toggle("indicator-dismissed", Boolean(closing));
    }
  });
  document.addEventListener("focusin", (event) => {
    closeOthers(event.target.closest?.(".indicator-explanation"));
  });
  document.addEventListener("focusout", (event) => {
    const detail = event.target.closest?.(".indicator-explanation");
    if (detail && !detail.contains(event.relatedTarget)) {
      detail.open = false;
      detail.classList.remove("indicator-dismissed");
    }
  });
  document.addEventListener("pointerover", (event) => {
    const detail = event.target.closest?.(".indicator-explanation");
    if (detail && !detail.contains(event.relatedTarget)) {
      detail.classList.remove("indicator-dismissed");
      closeOthers(detail);
    }
  });
})();
