// esbuild wrapper — bundles src/app.tsx into dist/assets/app.js
// and copies index.html + styles.css to dist/.
//
// Run: `node build.mjs`   (or `npm run build`)
// Watch: `node build.mjs --watch`

import { build, context } from "esbuild";
import { copyFileSync, mkdirSync, existsSync } from "node:fs";

const outdir = "dist";
const assetdir = `${outdir}/assets`;
if (!existsSync(assetdir)) mkdirSync(assetdir, { recursive: true });
copyFileSync("index.html", `${outdir}/index.html`);
copyFileSync("src/styles.css", `${assetdir}/styles.css`);

const opts = {
  entryPoints: ["src/app.tsx"],
  bundle: true,
  minify: true,
  sourcemap: true,
  target: "es2020",
  format: "esm",
  jsx: "automatic",
  jsxImportSource: "preact",
  outfile: `${assetdir}/app.js`,
  logLevel: "info",
  banner: {
    js: "/* local-ai-stack frontend — generated, do not edit */",
  },
};

if (process.argv.includes("--watch")) {
  const ctx = await context(opts);
  await ctx.watch();
  console.log("watching src/ for changes…");
} else {
  await build(opts);
}
