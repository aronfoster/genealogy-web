// Availability of synthetic review fixtures, shared by renderer and local server.
// This is NOT production authorization: no real private data may enter this app.
const publicPeople = new Set([
  "sr",
  "infant",
  "glazer",
  "measurer",
  "surveyor",
  "custodian",
  "carver",
  "potter",
  "engraver",
]);
const publicSources = new Set([
  "obituary",
  "infant",
  "draft",
  "archive",
  "memorial",
]);
const publicViews = new Set([
  "home",
  "search",
  "person",
  "sparse",
  "apprentice",
  "evidence",
  "sources",
  "restricted",
  "access",
  "privacy",
  "loading",
]);
export function isAvailableRoute(params) {
  const view = params.get("view") || "home";
  const family = params.get("projection") === "family";
  if (view === "withheld") return family;
  if (view === "profile" || view === "tree") {
    const person =
      view === "profile" ? params.get("person") : params.get("focus") || "sr";
    return publicPeople.has(person) || (family && person === "apprenticekin");
  }
  if (view === "source") {
    const source = params.get("record") || "obituary";
    return publicSources.has(source) || (family && source === "account");
  }
  return publicViews.has(view);
}
