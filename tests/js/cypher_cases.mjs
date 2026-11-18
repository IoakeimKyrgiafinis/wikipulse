// Runs the shared read-only cases (tests/fixtures/cypher_cases.json) against the
// browser's validator, site/cypher-check.js. Called by tests/test_demo_graph_js.py:
//   node tests/js/cypher_cases.mjs <site folder> <cases.json>
// Prints one JSON line {"passed": n, "failures": [...]} and exits 1 on any failure.
import { readFileSync } from "node:fs";
import { importFrom, stage } from "./stage.mjs";

const [siteDir, casesPath] = process.argv.slice(2);
const check = await importFrom(stage(siteDir), "cypher-check.js");
const cases = JSON.parse(readFileSync(casesPath, "utf-8"));
const failures = [];
let passed = 0;

function fail(kind, index, detail) {
  failures.push({ kind, index, ...detail });
}

cases.validate.forEach((c, index) => {
  const maxRows = c.max_rows ?? 25;
  let result;
  try {
    result = check.validateCypher(c.query, maxRows);
  } catch (error) {
    if (!(error instanceof check.UnsafeCypherError)) {
      fail("validate", index, { query: c.query, crashed: String(error) });
    } else if (!("error" in c)) {
      fail("validate", index, { query: c.query, refused: error.message });
    } else if (!new RegExp(c.error).test(error.message)) {
      fail("validate", index, { query: c.query, message: error.message, wanted: c.error });
    } else {
      passed += 1;
    }
    return;
  }
  const problems = [];
  if ("error" in c) problems.push(`accepted, wanted an error matching ${c.error}`);
  if ("equals" in c && result !== c.equals) problems.push("equals");
  if ("startswith" in c && !result.startsWith(c.startswith)) problems.push("startswith");
  if ("endswith" in c && !result.endsWith(c.endswith)) problems.push("endswith");
  if (problems.length) fail("validate", index, { query: c.query, result, problems });
  else passed += 1;
});

cases.bind_times.forEach((c, index) => {
  const result = check.bindTimes(c.query);
  if (result !== c.expected) fail("bind_times", index, { query: c.query, result });
  else passed += 1;
});

console.log(JSON.stringify({ passed, failures }));
process.exit(failures.length ? 1 : 0);
