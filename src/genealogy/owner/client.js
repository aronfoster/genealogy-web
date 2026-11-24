import { initializeApp } from "firebase/app";
import {
  initializeAuth,
  inMemoryPersistence,
  sendSignInLinkToEmail,
  isSignInWithEmailLink,
  signInWithEmailLink,
  signOut,
} from "firebase/auth";
const $ = (id) => document.getElementById(id);
const completionLink = location.href;
if (location.search) history.replaceState(null, "", "/");
let auth,
  current = null,
  page = 0,
  historyPage = 0,
  pending = null,
  busy = false,
  epoch = 0;
const actions = new Map();
function message(text) {
  $("message").textContent = text;
}
function clear() {
  epoch++;
  current = null;
  pending = null;
  actions.clear();
  $("action").replaceChildren();
  $("person").replaceChildren();
  $("note").value = "";
  $("person-query").value = "";
  $("verified").checked = false;
  $("member-title").textContent = "Member";
  $("state").textContent = "";
  $("members").replaceChildren();
  $("identities").replaceChildren();
  $("requests").replaceChildren();
  $("history").replaceChildren();
  $("detail").hidden = true;
  $("admin").hidden = true;
  $("signin").hidden = false;
}
function cookie() {
  return (
    document.cookie
      .split("; ")
      .find((x) => x.startsWith("__Host-owner-csrf="))
      ?.split("=")
      .slice(1)
      .join("=") || ""
  );
}
async function api(path, data) {
  const generation = epoch;
  const response = await fetch(path, {
    method: data ? "POST" : "GET",
    credentials: "same-origin",
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
    headers: data
      ? { "Content-Type": "application/json", "X-CSRF-Token": cookie() }
      : {},
    body: data ? JSON.stringify(data) : undefined,
  });
  const result = await response.json();
  if (path != "/api/bootstrap" && generation !== epoch)
    throw new Error("View changed. Refresh to continue.");
  if (!response.ok) {
    if ([401, 403, 503].includes(response.status)) clear();
    throw new Error(result.error || "Request failed");
  }
  return result;
}
function record(parent, text) {
  const el = document.createElement("div");
  el.className = "record";
  el.textContent = text;
  parent.append(el);
  return el;
}
function option(value, label) {
  const el = document.createElement("option");
  el.value = value;
  el.textContent = label;
  return el;
}
async function queue() {
  const generation = epoch;
  const result = await api(
    "/api/members?all=" + ($("all").checked ? "1" : "0") + "&offset=" + page,
  );
  if (generation !== epoch) return;
  $("admin").hidden = false;
  $("signin").hidden = true;
  $("members").replaceChildren();
  for (const m of result.members.slice(0, 50)) {
    const b = document.createElement("button");
    b.textContent =
      ((m.emails || []).join(", ") || m.preauthorization?.email || "Member") +
      " · " +
      m.state +
      (m.reconciled ? "" : " · needs reconciliation") +
      (m.open_requests ? " · requests waiting" : "");
    b.onclick = () => perform(() => detail(m.id));
    const li = document.createElement("li");
    li.append(b);
    $("members").append(li);
  }
  $("previous").disabled = page === 0;
  $("next").disabled = result.members.length <= 50;
}
async function detail(id, older = false) {
  const generation = epoch;
  const result = await api(
    "/api/member?id=" +
      encodeURIComponent(id) +
      "&offset=" +
      (older ? historyPage : 0),
  );
  if (generation !== epoch) return;
  if (!older) {
    historyPage = 0;
    current = result.member;
    pending = null;
    $("member-title").textContent =
      result.identities[0]?.original_email ||
      result.preauthorization?.email ||
      "Member";
    $("state").textContent =
      current.state +
      " · version " +
      current.version +
      (current.reconciled ? "" : " · CLI reconciliation required") +
      " · Person: " +
      (current.person_id || "unlinked");
    $("identities").replaceChildren();
    $("requests").replaceChildren();
    $("history").replaceChildren();
    $("action").replaceChildren();
    actions.clear();
    const add = (label, data) => {
      const key = String(actions.size);
      actions.set(key, data);
      $("action").append(option(key, label));
    };
    if (current.reconciled) {
      if (current.state === "pending")
        add("Approve access", { action: "approve" });
      if (current.state !== "revoked")
        add("Revoke access — blocked until reapproved", { action: "revoke" });
      else add("Reapprove access", { action: "reapprove" });
      add("Set or remove person link", { action: "link-person" });
    }
    if (result.preauthorization) {
      record(
        $("identities"),
        result.preauthorization.email +
          " · Preauthorized · " +
          (result.preauthorization.claimed ? "claimed" : "not yet claimed"),
      );
    }
    for (const i of result.identities) {
      record(
        $("identities"),
        i.original_email +
          " · " +
          i.project +
          " / " +
          i.uid +
          " · " +
          (i.active ? "active" : "retired"),
      );
      if (current.reconciled && i.active)
        add("Retire identity: " + i.original_email + " / " + i.uid, {
          action: "retire-binding",
          binding_id: i.binding_id,
        });
    }
    $("verified").checked = false;
    $("note").value = "";
    $("person").replaceChildren(option("", "Remove person link"));
    $("person-query").value = "";
    $("detail").hidden = false;
    for (const r of result.requests.slice(0, 50)) {
      if (!r.decided_at)
        add("Dismiss request " + r.id + " — they may ask again", {
          action: "dismiss-request",
          request_id: r.id,
        });
    }
    $("apply").disabled = busy || !actions.size;
    $("person-selection").hidden =
      actions.get($("action").value)?.action !== "link-person";
  }
  for (const r of result.requests.slice(0, 50)) {
    record(
      $("requests"),
      r.original_email +
        " · " +
        r.project +
        " / " +
        r.uid +
        "\n" +
        (r.conflict_member_id
          ? "Identity conflict — rebinding requires CLI verification.\n"
          : "") +
        (r.explanation || "No explanation supplied.") +
        "\n" +
        (r.decided_at ? "Closed " + r.decided_at : "Open") +
        " · " +
        r.id,
    );
  }
  for (const d of result.history.slice(0, 50)) {
    const li = document.createElement("li");
    li.className = "record";
    li.textContent =
      d.occurred_at +
      " · " +
      d.action +
      " · " +
      d.actor +
      "\n" +
      (d.from_state || "") +
      " → " +
      d.to_state +
      " · version " +
      d.version +
      "\n" +
      (d.note || "") +
      (d.action === "link-person"
        ? "\n" +
          (d.from_person || "unlinked") +
          " → " +
          (d.to_person || "unlinked")
        : "");
    $("history").append(li);
  }
  $("older").disabled =
    result.requests.length <= 50 && result.history.length <= 50;
}
async function perform(fn) {
  if (busy) return;
  busy = true;
  $("apply").disabled = true;
  try {
    await fn();
  } catch (e) {
    message(
      e.code
        ? "Unable to complete sign-in. Request a new link and try again."
        : e.message || "Temporarily unavailable",
    );
  } finally {
    busy = false;
    $("apply").disabled = !current || !actions.size;
  }
}
const sendLink = (e) => {
  e.preventDefault();
  perform(async () => {
    await sendSignInLinkToEmail(auth, $("email").value.trim(), {
      url: location.origin + "/",
      handleCodeInApp: true,
    });
    message("Sign-in link sent. Open it here and confirm your owner email.");
  });
};
$("login").onsubmit = sendLink;
function resetLogin() {
  $("login").onsubmit = sendLink;
  $("login").querySelector("button").textContent = "Send sign-in link";
  $("new-link").hidden = true;
}
$("new-link").onclick = resetLogin;
$("logout").onclick = async () => {
  // Logout must never be dropped because a queue refresh is still in flight.
  clear();
  resetLogin();
  try {
    await api("/api/logout", {});
    await signOut(auth);
    await api("/api/bootstrap");
    message("Signed out.");
  } catch {
    message("Session could not be cleared. Close this tab and retry sign-out.");
  }
};
$("refresh").onclick = () =>
  perform(async () => {
    if (current) await detail(current.id);
    await queue();
  });
$("all").onchange = () =>
  perform(async () => {
    page = 0;
    await queue();
  });
$("previous").onclick = () =>
  perform(async () => {
    page = Math.max(0, page - 50);
    await queue();
  });
$("next").onclick = () =>
  perform(async () => {
    page += 50;
    await queue();
  });
$("older").onclick = () =>
  perform(async () => {
    historyPage += 50;
    await detail(current.id, true);
  });
$("action").onchange = () => {
  $("person-selection").hidden =
    actions.get($("action").value)?.action !== "link-person";
  pending = null;
};
$("search").onclick = () =>
  perform(async () => {
    const result = await api(
      "/api/people?q=" + encodeURIComponent($("person-query").value),
    );
    $("person").replaceChildren(option("", "Remove person link"));
    for (const p of result.people.slice(0, 25))
      $("person").append(option(p.id, p.name + " · " + p.id));
    message(
      result.people.length > 25
        ? "More matches exist. Narrow your search."
        : "Select the intended person.",
    );
  });
$("decision").onsubmit = (e) => {
  e.preventDefault();
  perform(async () => {
    if (!current || !$("verified").checked) return;
    const selected = actions.get($("action").value);
    if (!selected) return;
    const data = {
      member_id: current.id,
      expected_version: current.version,
      ...selected,
      note: $("note").value,
      verified: true,
      ...(selected.action === "link-person"
        ? { person_id: $("person").value || null }
        : {}),
    };
    const fingerprint = JSON.stringify(data);
    if (!pending || pending.fingerprint !== fingerprint)
      pending = {
        fingerprint,
        data: { ...data, operation_id: crypto.randomUUID() },
      };
    const id = current.id;
    let result;
    try {
      result = await api("/api/decide", pending.data);
    } catch (e) {
      if (current && e.message.startsWith("Decision changed")) await detail(id);
      throw e;
    }
    await detail(id);
    await queue();
    message(
      result.outcome === "already-applied"
        ? "Already applied. Current record refreshed."
        : "Decision saved. Current record refreshed.",
    );
  });
};
// Never leave request prose in a hidden tab or a back/forward cache. A new read
// requires a fresh provider+owner check. No queue, email or token local storage.
addEventListener("pagehide", clear);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clear();
  else perform(queue);
});
addEventListener("pageshow", (e) => {
  if (e.persisted) {
    clear();
    perform(queue);
  }
});
setInterval(() => {
  if (!document.hidden && !busy && !$("admin").hidden)
    perform(async () => {
      await api("/api/status");
    });
}, 15000);
(async () => {
  try {
    const bootstrap = await api("/api/bootstrap");
    auth = initializeAuth(initializeApp(bootstrap.firebase), {
      persistence: inMemoryPersistence,
    });
    if (isSignInWithEmailLink(auth, completionLink)) {
      const link = completionLink;
      message("Confirm your owner email to complete sign-in.");
      $("login").querySelector("button").textContent = "Complete sign-in";
      $("new-link").hidden = false;
      $("login").onsubmit = (e) => {
        e.preventDefault();
        perform(async () => {
          let credential;
          try {
            credential = await signInWithEmailLink(
              auth,
              $("email").value.trim(),
              link,
            );
            await api("/api/exchange", {
              idToken: await credential.user.getIdToken(true),
            });
          } finally {
            await signOut(auth);
          }
          await api("/api/bootstrap");
          $("email").value = "";
          resetLogin();
          await queue();
          message("Signed in.");
        });
      };
    } else await queue();
  } catch (e) {
    message(e.message || "Unable to sign in.");
  }
})();
