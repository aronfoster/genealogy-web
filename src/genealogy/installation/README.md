# Using an installed genealogy archive

Install a locally supplied wheel or source distribution into a fresh Python 3.11+
virtual environment. Pass the path to the file you received to `python -m pip
install`. The source distribution includes its pinned build backend requirement
and lockfile. A local wheelhouse can supply dependencies for an offline install.

## Try the compiler

In an empty working directory, activate that environment and run the following
with a POSIX shell. This creates one invented person and a public projection.
It needs no database, cloud credentials, network connection or sibling checkout.

```sh
mkdir -p records/person assets inputs
person=$(genealogy allocate person)
printf '{"id":"%s","retired":false,"schema_version":1,"type":"person","visibility":"public"}\n' "$person" > "records/person/$person.json"
printf '%s\n' '{"version":1,"occurrences":[]}' > inputs/policy.json
printf '%s\n' '{"roots":[],"rationale":"Local synthetic example"}' > inputs/roots.json
printf '%s\n' '{"version":1,"contexts":{}}' > inputs/contexts.json
python -c 'from importlib.resources import files; from pathlib import Path; Path("inputs/rules.json").write_bytes(files("genealogy").joinpath("historical-names.json").read_bytes())'
genealogy validate records --asset-root assets
genealogy compile records public.json --build-year 2026 --audience public \
  --validation-profile synthetic --asset-root assets --name-policy inputs/policy.json \
  --family-roots inputs/roots.json --historical-name-rules inputs/rules.json \
  --reviewed-name-contexts inputs/contexts.json
```

For an actual collection supply its approved year, corpus, asset directory and
four reviewed input documents. The package supplies U.S. statistical cohorts
for 1880–1919. These support statistical inference and do not establish a person’s documented sex. Earlier-period extrapolation requires separately supplied rules.
Keep operational inputs, identity bindings, source bytes and credentials under
operator control. Synthetic output serves only as a software check.

## Reader and operator configuration

Set `GENEALOGY_SITE_NAME` for the reader and owner portal and
`GENEALOGY_ADMIN_CONTACT` for the reader’s profile help. The defaults are
“Family archive” and “the archive administrator”.

The reader listens on loopback. Supply `GENEALOGY_PUBLIC_DSN` for a
least-privileged PostgreSQL reader. The default authentication mode is public.
Family access requires an explicit HTTPS origin, Firebase web configuration,
CSRF secret, family and authorization database connections. The owner portal
uses a separate origin, Firebase project and operator privileges. Feedback
delivery is off by default. Its existing YouTrack integration requires the
reviewed project identity and receipt format of the operator installation.

For automated work, use invented data for examples and run validation from the
installed environment. Keep private provisioning, service configuration and
release commands in the operator checkout. A successful synthetic compile
does not establish private source-byte acceptance or authorize release.
