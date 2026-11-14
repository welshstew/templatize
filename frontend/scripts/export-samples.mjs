// Write the editor's samples to tests/quality/samples.json, so the Python
// suggestion-quality tests run on exactly what users see. `npm test` fails when
// the file is out of date; run `make samples` (or this script) to refresh it.
import { writeFileSync } from "node:fs";

import { SAMPLES } from "../src/sample.ts";

export const SAMPLES_JSON = new URL("../../tests/quality/samples.json", import.meta.url);

export function samplesJson() {
  const rows = SAMPLES.map(({ id, text, variants }) => ({ id, text, variants: variants ?? [] }));
  return JSON.stringify(rows, null, 2) + "\n";
}

if (import.meta.url === `file://${process.argv[1]}`) {
  writeFileSync(SAMPLES_JSON, samplesJson());
  console.log(`wrote ${SAMPLES.length} samples to tests/quality/samples.json`);
}
