#!/usr/bin/env node
// Fails if the built frontend references anything outside its own origin.
// XML namespace identifiers (never fetched) are allowed.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const root = process.argv[2] ?? "dist";
const ALLOWED = [
  /^https?:\/\/www\.w3\.org\/(1999|2000)\/(xhtml|svg|xlink)$/,
  /^https?:\/\/www\.w3\.org\/1998\/Math\/MathML$/,
  /^https?:\/\/www\.w3\.org\/XML\/1998\/namespace$/,
];
// absolute URLs, protocol-relative URLs in src/href/url(), and @import of remote CSS
const PATTERNS = [
  /\bhttps?:\/\/[^\s"'`)<>\\]+/g,
  /(?:src|href)\s*=\s*["']\/\/[^"']+/g,
  /url\(\s*["']?\/\/[^)]+\)/g,
  /@import\s+(?:url\()?["']?(?:https?:)?\/\//g,
];

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* walk(p);
    else yield p;
  }
}

let bad = 0;
let files = 0;
for (const file of walk(root)) {
  if (!/\.(html|js|mjs|css|svg|json|txt|webmanifest)$/.test(file)) continue;
  files++;
  const text = readFileSync(file, "utf8");
  for (const re of PATTERNS) {
    for (const m of text.matchAll(re)) {
      const url = m[0];
      if (ALLOWED.some((a) => a.test(url))) continue;
      console.error(`external reference in ${relative(root, file)}: ${url}`);
      bad++;
    }
  }
}
if (files === 0) {
  console.error(`no built files found in ${root}`);
  process.exit(1);
}
if (bad) {
  console.error(`${bad} external reference(s) found; the UI must work fully offline`);
  process.exit(1);
}
console.log(`offline check: ${files} built files, no external references`);
