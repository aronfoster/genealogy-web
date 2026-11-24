# Bundled fonts

The prototype self-hosts these unmodified Latin WOFF2 variable-font subsets:

- Inter normal, from `@fontsource-variable/inter@5.3.0` (`inter-latin-wght-normal.woff2`).
- Source Serif 4 normal and italic, from `@fontsource-variable/source-serif-4@5.3.0` (`source-serif-4-latin-wght-{normal,italic}.woff2`).

Downloaded from the versioned npm packages. Their SIL Open Font License notices are included beside the fonts. Local filenames omit `wght`; font bytes are unchanged. CSS uses the internal aliases Archive Sans / Archive Serif; this does not modify the fonts or their naming tables.

No network font service or system-installed Georgia/Inter is required. Capture tooling waits for these font faces to load; their bytes are included in the screenshot input hash. Linux x64 with the pinned packaged Chromium is the reference renderer. OS/browser rasterization differences outside that reference environment are not guaranteed pixel-identical.
