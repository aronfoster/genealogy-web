import { build } from "esbuild";
await build({
  entryPoints: ["src/genealogy/owner/client.js"],
  outfile: "src/genealogy/owner/owner.js",
  bundle: true,
  minify: true,
  format: "esm",
  target: ["es2022"],
  legalComments: "inline",
});
