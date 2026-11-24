import { build } from "esbuild";
await build({
  entryPoints: ["src/genealogy/auth/access.js"],
  outfile: "src/genealogy/web/static/access.js",
  bundle: true,
  minify: true,
  format: "esm",
  target: ["es2022"],
  legalComments: "inline",
});
