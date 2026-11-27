# Genealogy web

A genealogy reader and compiler with separate public and family views. The
[installation guide](src/genealogy/installation/README.md) includes a complete
example that builds a public projection from invented records.

First-party software uses the [MIT license](LICENSE). The
[attribution record](src/genealogy/notices/ATTRIBUTION.txt) identifies the
bundled libraries, fonts and statistical data and their respective terms.

## Projection compatibility

The compiler validates canonical schema version 1 and produces separate public
and family documents. The packaged projection schema defines the serving rows.
A private schema addition has no default renderer. The compiler derives supported
display fields after applying the audience, evidence and publication rules.

| Target profile | Output |
| --- | --- |
| `baseline-v1` | Original envelope and serving rows. Omit profile extensions and their exclusive catalog dependencies. |
| `profile-v1` | Declare both required capabilities, even with empty collections. Eligible current, cited assertions can supply a favorite color or a portrait. |

Both profiles enforce the deceased-only public audience. A living or possibly living
person with a public grant can appear only in an eligible family view. This deliberately
tightens the released producer's policy even for baseline-shaped output. Use the capable
producer for that input when staging rows for an older reader.

Use `genealogy projection-descriptor CORPUS OUTPUT --inputs INPUTS --projection-profile PROFILE --target-runtime COMMIT` to prepare the two document hashes, contract hash,
publication input identity and reader target for private review. The existing
`genealogy import` command accepts the approved bytes via `--descriptor`,
an independently pinned `--descriptor-sha256`, and the same profile and runtime.
It recompiles and verifies both documents before opening the import transaction.
A missing or inconsistent pin rejects the candidate. The baseline importer predates
this protocol. Use the capable importer when staging baseline-shaped output for
a released reader. Change required field types, meanings, or policy semantics
only through a new supported profile and an explicit migration. Install the
dual-reading software before enabling a new producer profile.

A portrait uses a cited `person.photo` assertion and a stripped RGB PNG
derivative. Both source and derivative need suitable rights, and every identified
subject needs a current scoped grant. An eligible portrait is delivered from
`GENEALOGY_PORTRAIT_ROOT` by content digest through an audience-scoped route.
Keep that directory outside all static mounts. The route verifies current access
and the byte digest on each request. Profile-only sources and citations can be
marked `profile_only` so their catalog entries disappear when unsupported.
The thumbnail route serves the same verified derivative at its intrinsic aspect ratio.
A field's source links lead to its supporting citations in the same audience.
The original and its storage locator stay outside both serving projections.
