import { isAvailableRoute } from "./route-policy.mjs";
// Synthetic review projections only. Production must filter data before delivery.
const q = new URLSearchParams(location.search);
const view = q.get("view") || "home";
const state = q.get("state") || "";
const family =
  q.get("projection") === "family" ||
  (view === "privacy" && state === "family");
const main = document.querySelector("#main");
const has = (object, key) => Object.hasOwn(object, key);
const escape = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const route = (name, params = {}) =>
  `?${new URLSearchParams({ view: name, ...(family ? { projection: "family" } : {}), ...params })}`;
const link = (name, label, params = {}) =>
  `<a href="${escape(route(name, params))}">${label}</a>`;
const crumb = (label) =>
  `<div class="breadcrumbs">${link("home", "Home")} / ${label}</div>`;
const badge = (kind, text) => `<span class="badge ${kind}">${text}</span>`;
const dimensions = (
  status,
  quality,
  origin,
  identity = "Identified person",
  qualityLabel = "Evidence strength",
) => `<dl class="dimensions">
  <div class="dimension"><dt>Research status</dt><dd>${status}</dd></div>
  <div class="dimension"><dt>${qualityLabel}</dt><dd>${quality}</dd></div>
  <div class="dimension"><dt>How we know</dt><dd>${origin}</dd></div>
  <div class="dimension"><dt>Identity</dt><dd>${identity}</dd></div>
</dl>`;

const people = {
  sr: {
    name: "Vela Cinderfin Sr.",
    dates: "born 1784 or 1786",
    cue: "Keeper of the east kiln",
    place: "Glassharbor",
    parents: ["measurer", "surveyor"],
    spouses: ["custodian"],
    children: ["infant", "glazer"],
  },
  infant: {
    name: "Vela Cinderfin Jr.",
    dates: "1814\u20131814",
    cue: "Appears once in the tidebook",
    place: "Glassharbor",
    parents: ["sr", "custodian"],
    spouses: [],
    children: [],
    siblings: ["glazer"],
  },
  glazer: {
    name: "Vela Amberwheel",
    dates: "1818\u20131893",
    cue: "A glaze maker with two recorded names",
    place: "Glassharbor",
    parents: ["sr", "custodian"],
    spouses: [],
    children: [],
    siblings: ["infant"],
  },
  measurer: {
    name: "Pavo Flintreed",
    dates: "1751\u20131824",
    cue: "Maker of clay measures",
    place: "Glassharbor",
    parents: [],
    spouses: ["surveyor"],
    children: ["sr"],
  },
  surveyor: {
    name: "Inka Frostcup",
    dates: "1756\u20131829",
    cue: "Surveyor of kiln marks",
    place: "Glassharbor",
    parents: [],
    spouses: ["measurer"],
    children: ["sr"],
  },
  custodian: {
    name: "Kestrel Vessel",
    dates: "1789\u20131855",
    cue: "Custodian of the tidebook",
    place: "Glassharbor",
    parents: [],
    spouses: ["sr"],
    children: ["infant", "glazer"],
  },
  carver: {
    name: "Fenn Glazeward",
    dates: "1911–1996",
    cue: "A life remembered through a seal",
    place: "Example Cove",
    parents: [],
    spouses: ["potter", "engraver"],
    children: family ? ["apprenticekin"] : [],
  },
  potter: {
    name: "Cairn Glazeward",
    dates: "1909–1952",
    cue: "Spouse of Fenn · 1934–1952",
    place: "Example Cove",
    parents: [],
    spouses: ["carver"],
    children: [],
  },
  engraver: {
    name: "Linden Glazeward",
    dates: "1914–1992",
    cue: "Spouse of Fenn · from about 1958",
    place: "Example Cove",
    parents: [],
    spouses: ["carver"],
    children: [],
  },
  ...(family
    ? {
        apprenticekin: {
          name: "Aster Glazeward",
          dates: "Living",
          cue: "Child of Fenn",
          place: "Example Cove",
          parents: ["carver"],
          spouses: [],
          children: [],
        },
      }
    : {}),
};
const personRoute = (id) =>
  route(
    id === "sr"
      ? "person"
      : id === "infant"
        ? "sparse"
        : id === "glazer"
          ? "apprentice"
          : "profile",
    { person: id },
  );
const personLink = (id, label) =>
  `<a href="${escape(personRoute(id))}">${label || people[id].name}</a>`;
const searchForm = (query = "") =>
  `<form class="search-box" role="search"><input type="hidden" name="view" value="search">${family ? '<input type="hidden" name="projection" value="family">' : ""}<label class="sr-only" for="people-query">Search people</label><input id="people-query" name="query" value="${escape(query)}" placeholder="Try Vela Cinderfin"><button>Search</button></form>`;

function home() {
  return `<section class="hero"><div><p class="eyebrow">Glassharbor workshop archive</p><h1>The people behind the kiln marks</h1><p class="lede">Two workshop registers and a tidebook trace a household of glaze makers.</p>${searchForm()}</div><div class="portrait-art" role="img" aria-label="Glassharbor kiln seal"><div class="caption"><strong>East kiln</strong><br><small>Workshop rolls, 1784–1893</small></div></div></section>
  <section class="section"><h2>Explore the workshop</h2><div class="cards"><article class="card"><h3>${link("person", "The first kiln keeper")}</h3><p>Two marks disagree on the keeper’s birth year.</p></article><article class="card"><h3>${link("apprentice", "A second signature")}</h3><p>A maker adopted a trade name on the glaze rolls.</p></article><article class="card"><h3>${link("evidence", "Read the roll evidence")}</h3><p>Compare inscriptions, corrected readings and an unresolved identity.</p></article></div></section>`;
}
function search() {
  const query = q.get("query") ?? "Vela Cinderfin";
  const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const matches =
    state === "zero"
      ? []
      : Object.entries(people).filter(([id, p]) =>
          terms.every((t) =>
            `${p.name} ${p.place} ${p.dates} ${id === "glazer" ? "Vela Cinderfin Jr." : ""}`
              .toLowerCase()
              .includes(t),
          ),
        );
  return `<div class="page">${crumb("People")}<header class="page-title"><p class="eyebrow">Find a person</p><h1>${matches.length ? "People matching your search" : "No matching people"}</h1><p class="lede">${matches.length ? "Names, dates, places, and family context help distinguish people." : "Try another spelling, a place, or a year."}</p>${searchForm(query)}</header>
  ${
    matches.length
      ? `<div class="result-list">${matches
          .map(
            ([id, p]) =>
              `<a class="result" href="${escape(personRoute(id))}"><span class="avatar" aria-hidden="true">${p.name
                .split(" ")
                .slice(0, 2)
                .map((x) => x[0])
                .join(
                  "",
                )}</span><span><h2>${p.name}</h2><p>${p.cue}</p>${id === "glazer" ? "<p>Recorded as Vela Cinderfin Jr. (kiln roster)</p>" : ""}<p>${p.place}</p></span><span class="life">${p.dates}</span></a>`,
          )
          .join("")}</div>`
      : '<section class="empty"><h2>We didn’t find a match</h2><p>Check the spelling or try fewer words.</p></section>'
  }</div>`;
}

function kin(id) {
  const p = people[id];
  return [
    ["Parents", p.parents],
    ["Spouses", p.spouses],
    ["Children", p.children],
    ["Siblings", p.siblings || []],
  ]
    .filter(([, ids]) => ids.length)
    .map(
      ([label, ids]) =>
        `<section class="side-card"><h2>${label}</h2><div class="kin">${ids.map((other) => personLink(other, `<strong>${people[other].name}</strong><br>${people[other].dates}`)).join("")}</div></section>`,
    )
    .join("");
}
function person(id = "sr") {
  if (!has(people, id)) return unknown();
  const p = people[id];
  let content;
  if (id === "sr")
    content = `<h2>The kiln marks</h2><p class="prose">The workshop roll identifies Vela as the keeper of the east kiln.</p><ol class="timeline"><li><time>1784 or 1786</time><strong>Conflicting birth inscriptions</strong> ${link("evidence", "Compare the roll readings", { anchor: "birth" })}</li><li><time>1814</time>${personLink("infant")} appears in the tidebook.</li><li><time>1818</time>${personLink("glazer")} joins the household.</li></ol>`;
  else if (id === "infant")
    content = `<h2>One tidebook entry</h2><p class="prose">The keeper entered a birth and a death on the same low tide.</p>${dimensions("Current conclusion", "Moderate evidence · B", "Later transcript")}<p>${link("source", "Read the entry", { record: "infant" })}</p>`;
  else if (id === "glazer")
    content = `<h2>Names in the workshop rolls</h2><p class="prose">The kiln roster and signed glaze card carry the same maker’s mark under two names.</p><ol class="timeline"><li><time>1818</time>First listed in the tidebook</li><li><time>1841</time>Signed the amber glaze card</li><li><time>1893</time>Last listed in the kiln register</li></ol>${link("evidence", "Compare the signatures", { anchor: "persona" })}`;
  else if (id === "carver")
    content = `<h2>Remembered through a seal</h2>${privacyClaim()}<h2>Family relationships</h2><p>Cairn and Fenn were married from 1934 to 1952. The later marriage to Linden began about 1958.</p>${family ? `<p>${personLink("apprenticekin")} is remembered as Fenn’s child.</p>${dimensions("Current conclusion", "Useful but unproved · B", "Family testimony")}` : ""}`;
  else
    content = `<h2>Known details</h2><p class="prose">${p.cue}. ${p.place}.</p>${id === "apprenticekin" ? `${dimensions("Current conclusion", "Useful but unproved · B", "Family testimony")}<p>Recorded in the synthetic family account. This relationship is useful, while a direct civil record is still lacking.</p>` : `<p>${link("evidence", "Read the relationship evidence")}</p>`}`;
  return `<div class="page">${crumb("People")}<header class="profile-head"><div><p class="eyebrow">${id === "carver" || id === "apprenticekin" || id === "potter" || id === "engraver" ? "Glazeward family" : "East kiln household"}</p><h1>${p.name}</h1><p class="former">${p.cue}</p><div class="factline"><div><strong>Life dates</strong><br>${p.dates}${id === "sr" ? `<br>${badge("conflict", "Sources disagree")}` : ""}</div><div><strong>Places</strong><br>${p.place}</div></div></div><a class="quiet-button" href="${escape(route("tree", { focus: id }))}">View family context</a></header><div class="layout"><article>${content}</article><aside class="sidebar">${kin(id)}<section class="side-card"><h2>Sources</h2>${link("sources", "Browse source gallery")}</section></aside></div></div>`;
}

function evidence() {
  const cases = [
    [
      "entry",
      "accepted",
      "Current conclusion",
      "The glaze card names Vela’s parents",
      "Directly documented · A",
      "Kiln entry",
      "Both names appear beside the maker’s seal.",
    ],
    [
      "birth",
      "conflict",
      "Sources disagree",
      "Two dates beneath the kiln seal",
      "Conflicting evidence",
      "Roll comparison",
      "The roll reads 1784. The kiln inscription reads 1786.",
    ],
    [
      "derived",
      "accepted",
      "Working date",
      "Dating the final firing",
      "Moderate evidence · B",
      "Researcher calculation",
      "Three days before the recorded tide places the firing on the twelfth.",
    ],
    [
      "corrected",
      "corrected",
      "Corrected reading",
      "The tally marks count crates",
      "Strong evidence · A",
      "Record comparison",
      "A second roll labels the same marks as crates rather than people.",
    ],
    [
      "identity",
      "candidate",
      "Possible connection",
      "Did two makers share a seal?",
      "Early lead · D",
      "Researcher hypothesis",
      "The north and east kilns used similar stamps. The signatures differ.",
    ],
    [
      "persona",
      "accepted",
      "Current conclusion",
      "Two signatures on one glaze recipe",
      "Strong evidence · A",
      "Record comparison",
      "The kiln roster calls the maker Vela Cinderfin Jr. The signed card reads Vela Amberwheel.",
    ],
    [
      "limited",
      "candidate",
      "Unresolved",
      "An apprentice on the west bank",
      "Limited evidence · C",
      "Copied workshop index",
      "Only the later index supplies a relationship.",
    ],
  ];
  if (family)
    cases.push([
      "testimony",
      "accepted",
      "Working relationship",
      "An oral account of the workshop",
      "Moderate evidence · B",
      "Family testimony",
      "The family edition preserves a separate recollection.",
    ]);
  return `<div class="page">${crumb("Evidence")}<header class="page-title"><h1>Workshop evidence</h1></header><div class="evidence">${cases.map(([id, kind, status, title, strength, origin, detail]) => `<article class="claim" id="${id}">${badge(kind, status)}<h2>${title}</h2>${dimensions(status, strength, origin)}<details open><summary>Read the supporting entry</summary><p>${detail}</p>${link("source", "Workshop citation")}</details></article>`).join("")}</div></div>`;
}
function tree() {
  const id = q.get("focus") || "sr";
  const p = people[id];
  if (!has(people, id)) return unknown();
  const referenceId = has(people, q.get("reference")) ? q.get("reference") : "";
  const group = (label, ids) =>
    ids.length
      ? `<section class="tree-col"><h2 class="tree-label">${label}</h2>${ids.map((other) => `<a class="node" href="${escape(route("tree", { focus: other, ...(referenceId ? { reference: referenceId } : {}) }))}"><strong>${people[other].name}</strong><small>${people[other].dates}</small><small>${people[other].cue}</small></a>`).join("")}</section>`
      : "";
  let relation = "";
  if (referenceId) {
    const r = people[referenceId];
    const label =
      id === referenceId
        ? "the reference person"
        : r.children.includes(id)
          ? "a child"
          : r.parents.includes(id)
            ? "a parent"
            : r.spouses.includes(id)
              ? "a spouse"
              : (r.siblings || []).includes(id)
                ? "a sibling"
                : null;
    relation = `<div class="notice"><strong>Reference: ${r.name}</strong><p>${label ? `${p.name} is ${label}${id === referenceId ? "" : ` of ${r.name}`}${id !== referenceId && /[.!?]$/.test(r.name) ? "" : "."}` : "This view calculates only immediate relationships. Longer paths are not calculated here; this does not mean these people are unrelated."}</p></div>`;
  }
  return `<div class="page">${crumb("Family context")}<header class="page-title"><p class="eyebrow">Focused family context</p><h1>${p.name} and nearby family</h1><p class="lede">Choose a relative to focus on their family. Use Back to retrace your path.</p><p>${personLink(id, "Read profile")} · ${link("tree", "Use this person as relationship reference", { focus: id, reference: id })}</p></header>${relation}<nav class="tree" aria-label="Family members around ${p.name}">${group("Parents", p.parents)}<section class="tree-col"><h2 class="tree-label">Current focus</h2><div class="node focus"><strong>${p.name}</strong><small>${p.dates}</small></div></section>${group("Spouses", p.spouses)}${group("Siblings", p.siblings || [])}${group("Children", p.children)}</nav></div>`;
}

const sourceRecords = {
  obituary: {
    title: "East kiln roster",
    date: "1818",
    citation: "Invented Glassharbor collection, east kiln roster.",
    url: "https://example.org/workshop/obituary",
    excerpt: "The workshop register names both parents.",
    rights: "Invented transcription.",
    note: "Workshop record",
    label: "East kiln roster",
  },
  infant: {
    title: "Low tide entry",
    date: "1814",
    citation: "Invented Glassharbor collection, low tide entry.",
    url: "https://example.org/workshop/infant",
    excerpt: "Two marks record the same short life.",
    rights: "Invented transcription.",
    note: "Workshop record",
    label: "Low tide entry",
  },
  draft: {
    title: "Signed glaze card",
    date: "1841",
    citation: "Invented Glassharbor collection, signed glaze card.",
    url: "https://example.org/workshop/draft",
    excerpt: "A second signature carries the east kiln seal.",
    rights: "Invented transcription.",
    note: "Workshop record",
    label: "Signed glaze card",
  },
  archive: {
    title: "Restricted tidebook",
    date: "1784\u20131786",
    citation: "Invented Glassharbor collection, restricted tidebook.",
    url: "https://example.org/workshop/archive",
    excerpt: "Only the cover has been described.",
    rights: "Catalogue metadata only.",
    note: "Holding record",
    label: "Restricted tidebook",
  },
  memorial: {
    title: "Fenn Glazeward memorial (synthetic)",
    date: "1996",
    citation:
      "Synthetic Example Cove memorial, p. 1. This document and all names in it are invented.",
    excerpt: "Fenn Glazeward carved the workshop seal.",
    rights:
      "The public edition is an independently authorized historical excerpt.",
    note: "Historical excerpt",
    label: "Memorial excerpt",
  },
  ...(family
    ? {
        account: {
          title: "Glazeward family account (synthetic)",
          date: "Date not recorded",
          citation: "Synthetic family testimony, statement 1.",
          excerpt:
            "Aster Glazeward recalls Fenn cutting the stamp from green stone.",
          rights: "Available to approved family in this synthetic fixture.",
          note: "Family account",
          label: "Family account",
        },
      }
    : {}),
};
function sources() {
  return `<div class="page">${crumb("Sources")}<header class="page-title"><p class="eyebrow">Source gallery</p><h1>Records behind the stories</h1><p class="lede">Read source summaries and follow available links to the holding archive.</p></header><div class="source-grid">${Object.entries(
    sourceRecords,
  )
    .map(
      ([id, s]) =>
        `<article class="card"><div class="thumb record" role="img" aria-label="${s.label}; typographic cover">${s.label}</div><h2>${s.title}</h2><p>${s.date} · ${s.note}</p>${link("source", "Open document details", { record: id })}</article>`,
    )
    .join("")}</div></div>`;
}
function source(restricted = false) {
  const id = restricted ? "archive" : q.get("record") || "obituary";
  const s = sourceRecords[id];
  if (!has(sourceRecords, id)) return unknown();
  return `<div class="page">${crumb("Sources / Document detail")}<div class="source-detail"><section class="document"><p class="eyebrow">${s.note}</p><h2>${s.label}</h2><p>${s.excerpt}</p>${id === "memorial" && family ? "<p><strong>Family edition</strong></p><p>Survived by Aster Glazeward, who remembered the carving sessions.</p>" : ""}</section><article><p class="eyebrow">Source detail</p><h1>${s.title}</h1><dl><dt>Date</dt><dd>${s.date}</dd><dt>Citation</dt><dd>${s.citation}</dd><dt>Publication</dt><dd>${s.rights}</dd></dl>${s.url ? `<a class="button" href="${s.url}">Verify with holding archive</a>` : ""}<p>${link("sources", "Return to source gallery")}</p></article></div></div>`;
}

function privacyClaim() {
  return `<p class="prose">Fenn Glazeward carved the workshop seal.</p>${family ? `<article class="claim"><span class="badge accepted">Current conclusion</span><h3>Evidence for the carving sessions</h3>${dimensions("Current conclusion", "Useful but unproved · B", "Family testimony")}<details open><summary>Read the family account</summary><p>Aster Glazeward remembers the carving bench. All identities and the testimony in this example are synthetic.</p>${link("source", "Read source detail", { record: "account" })}</details></article>` : ""}`;
}
function privacy() {
  return person("carver");
}
function access() {
  const s = state || "signin";
  const copy = {
    signin: ["Family sign in", "Enter your email to receive a sign-in link."],
    check: [
      "Check your email",
      "If a link can be sent, it will arrive shortly. Use it to continue.",
    ],
    request: [
      "Request family access",
      "Briefly describe your connection to the family.",
    ],
    pending: [
      "Request received",
      "Your request is awaiting review. You can continue exploring the public archive.",
    ],
    approved: ["Welcome back", "Your family access is approved."],
    denied: [
      "Access unavailable",
      "This account does not currently have family access. Public stories remain available.",
    ],
    revoked: [
      "Your session ended",
      "Sign in again or continue exploring the public archive.",
    ],
  };
  const c = has(copy, s) ? copy[s] : copy.signin;
  return `<div class="page"><section class="access-wrap"><p class="eyebrow">Family access</p><h1>${c[0]}</h1><p>${c[1]}</p>${s === "signin" || s === "request" ? `<form action=""><input type="hidden" name="view" value="access"><input type="hidden" name="state" value="${s === "signin" ? "check" : "pending"}"><label for="email">Email address</label><input id="email" type="email" required autocomplete="email">${s === "request" ? '<label for="connection">Your family connection</label><input id="connection" required maxlength="160">' : ""}<p><button class="button">${s === "signin" ? "Email me a sign-in link" : "Send request"}</button></p></form>` : s === "check" ? link("access", "Continue after sign-in", { state: "request" }) : s === "approved" ? `<a class="button" href="?view=privacy&projection=family">Continue to the archive</a>` : link("home", "Explore public stories")}<p>${link("home", "Return home")}</p></section></div>`;
}
function unknown() {
  return `<div class="page"><section class="not-found"><p class="code">404</p><h1>We can’t find that page</h1><p>The address may be incorrect or the page may no longer be available.</p><a class="button" href="?view=home">Return home</a></section></div>`;
}
function loading() {
  return '<div class="page"><h1>Family archive</h1><p role="status">Loading the archive…</p><div class="loading-line" aria-hidden="true"></div></div>';
}
const views = {
  home,
  search,
  person: () => person("sr"),
  sparse: () => person("infant"),
  apprentice: () => person("glazer"),
  profile: () => person(q.get("person")),
  evidence,
  tree,
  sources,
  source,
  restricted: () => source(true),
  access,
  privacy,
  loading,
  unknown,
  withheld: family ? () => person("apprenticekin") : unknown,
};
main.innerHTML = (
  isAvailableRoute(q) && has(views, view) ? views[view] : unknown
)();
// Export the actual route inventory for browser validation, not a duplicate text list.
document.documentElement.dataset.routes = Object.keys(views).sort().join(",");
const heading = main.querySelector("h1");
const unavailable = heading?.textContent === "We can’t find that page";
const title = unavailable
  ? "Page not found"
  : heading?.textContent || "Glassharbor Workshop Archive";
document.title = `${title} · Glassharbor Workshop Archive`;
document.querySelector('meta[property="og:title"]').content = document.title;
if (family && !unavailable) {
  for (const a of document.querySelectorAll(
    '.site-header a[href^="?"], footer a[href^="?"]',
  )) {
    const params = new URLSearchParams(a.search);
    params.set("projection", "family");
    a.search = params;
  }
  const panel = document.createElement("aside");
  panel.className = "review-controls";
  panel.setAttribute("aria-label", "Local review controls");
  panel.innerHTML =
    '<strong>Local review only</strong> <button type="button">Simulate session expiry</button>';
  document.body.append(panel);
  panel
    .querySelector("button")
    .addEventListener("click", () =>
      location.replace("?view=access&state=revoked"),
    );
}
if (view === "tree" && heading && !unavailable) {
  heading.tabIndex = -1;
  heading.focus({ preventScroll: true });
}
if (q.get("anchor")) document.getElementById(q.get("anchor"))?.scrollIntoView();
