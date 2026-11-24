const form = document.querySelector("[data-feedback-form]");
if (form) {
  const send = form.querySelector('[type="submit"]');
  const another = form.querySelector("[data-feedback-new]");
  const message = form.elements.message;
  const status = form.querySelector('[role="status"]');
  const retry = form.querySelector(".feedback-retry");
  const signin = form.querySelector("[data-feedback-signin]");
  const helper = form.querySelector("#feedback-helper");
  const helpers = {
    correction:
      "Tell us what could be corrected and what you think it should say. It's fine to be unsure.",
    add_person:
      "Tell us who is missing or how the people are related. Does the person (or a parent for a minor) agree to appear in the signed-in archive? Suggestions are reviewed before anything is published.",
    wording:
      "Tell us what was hard to understand or see, and what might make it clearer.",
    problem:
      "Tell us what you were trying to do and what happened. Include any error you saw, if you can.",
    other: "Share whatever you think would help our family history.",
  };
  const failed =
    "We couldn't confirm delivery. Your message is still here. Keep this page open and try again to check the same suggestion.";
  let key = crypto.randomUUID();
  let pending = null;
  let sending = false;
  let received = false;
  let conflict = false;

  function lock(locked) {
    message.readOnly = locked;
    for (const input of form.querySelectorAll("input")) input.disabled = locked;
  }
  form.addEventListener("change", () => {
    helper.textContent = helpers[form.elements.kind.value];
  });
  window.addEventListener("beforeunload", (event) => {
    if (!received && message.value.trim()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending || received || conflict) return;
    if (pending === null) {
      // Pasted invisible characters should not make a family member's draft
      // impossible to send. Show the cleaned text before freezing the payload.
      message.value = message.value
        .replace(/\t/gu, " ")
        .replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/gu, "");
    }
    if (!message.value.trim()) {
      status.textContent = "Please tell us a little more before sending.";
      message.focus();
      return;
    }
    const reconciling = pending !== null;
    signin.hidden = true;
    sending = true;
    send.disabled = true;
    send.textContent = "Sending…";
    status.textContent = "Sending your suggestion…";
    form.setAttribute("aria-busy", "true");
    lock(true);
    try {
      const bootstrap = await fetch("/api/access/bootstrap", {
        credentials: "same-origin",
      });
      if (!bootstrap.ok) throw new Error("Bootstrap unavailable");
      const { csrf } = await bootstrap.json();
      if (typeof csrf !== "string" || !csrf) throw new Error("No CSRF token");
      // Freeze the submitted payload after the first POST attempt. If its receipt
      // is lost, edits with the same UUID could be mistaken for the old message.
      pending ??= {
        clientKey: key,
        kind: form.elements.kind.value,
        pagePath: form.elements.includePage?.checked
          ? form.elements.includePage.value
          : null,
        message: message.value,
      };
      const response = await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        credentials: "same-origin",
        body: JSON.stringify(pending),
      });
      if (response.status === 429) {
        status.textContent =
          "You've sent several messages in a short time. Please wait a few minutes.";
        return;
      }
      if ([400, 401, 403].includes(response.status)) {
        // A refusal settles only this attempt. An earlier lost receipt may
        // already represent a committed issue, so retain that payload and UUID.
        if (!reconciling) pending = null;
        if (response.status === 401) {
          status.textContent =
            "Please sign in again in the new tab, then return here and send this same suggestion. Your message is still here.";
          signin.hidden = false;
        } else if (response.status === 403) {
          status.textContent =
            "Your access couldn't be verified. Your message is still here. Please try again, or contact the archive administrator.";
        } else {
          status.textContent = reconciling
            ? "We couldn't confirm your earlier submission. Keep this page open and contact the archive administrator before making changes."
            : "Your suggestion couldn't be accepted. Please check your message (up to 4000 characters), or contact the archive administrator.";
        }
        return;
      }
      if (response.status === 409) {
        // Ownership conflicts need administrator help. Rotating the key here
        // could duplicate an earlier submission under a different sign-in.
        conflict = true;
        status.textContent =
          "We couldn't confirm this suggestion. Keep this page open, copy your message, and contact the archive administrator before sending it again.";
        return;
      }
      if (response.status === 202) {
        status.textContent =
          "Your suggestion may have been received, but we couldn't confirm it yet. Keep this page open and try again to check it. If it stays unconfirmed, contact the archive administrator with this reference: " +
          key
            .slice(0, 8)
            .toUpperCase()
            .replace(/(.{4})/u, "$1-") +
          ".";
        return;
      }
      if (response.status === 503) {
        const result = await response.json();
        status.textContent =
          result.delivery === "rejected"
            ? "Your message wasn't sent yet. It's still here. Please try sending it again."
            : failed;
        return;
      }
      if (response.status !== 200) {
        throw new Error("Not received");
      }
      const receipt = await response.json();
      if (
        typeof receipt.reference !== "string" ||
        !/^[A-F0-9]{4}-[A-F0-9]{4}$/.test(receipt.reference) ||
        !/^GEN-\d+$/.test(receipt.issue)
      )
        throw new Error("Invalid receipt");
      received = true;
      status.textContent = `Thank you. Reference ${receipt.reference}. Your suggestion has been received for review; nothing changes automatically.`;
      send.hidden = true;
      another.hidden = false;
      retry.hidden = true;
      status.focus();
    } catch {
      status.textContent = failed;
    } finally {
      sending = false;
      form.removeAttribute("aria-busy");
      send.disabled = received || conflict;
      send.textContent = "Send suggestion";
      lock(received || pending !== null);
      if (!received) {
        retry.hidden = pending === null || conflict;
        if (conflict) status.focus();
        else send.focus();
      }
    }
  });
  another.addEventListener("click", () => {
    form.reset();
    key = crypto.randomUUID();
    pending = null;
    received = false;
    lock(false);
    send.hidden = false;
    send.disabled = false;
    another.hidden = true;
    status.textContent = "";
    retry.hidden = true;
    helper.textContent = helpers.correction;
    form.querySelector('[value="correction"]').focus();
  });
  send.disabled = false;
}
