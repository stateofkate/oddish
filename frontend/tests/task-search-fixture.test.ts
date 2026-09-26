import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { parseTaskSearch } from "../src/lib/tag-query.ts";

// Shared with oddish/tests/test_cli_qa.py, which checks the `oddish qa`
// copy of this parser against the same cases.
const cases: { search: string; expected: unknown }[] = JSON.parse(
  readFileSync(new URL("./fixtures/task-search.json", import.meta.url), "utf8")
);

test("parseTaskSearch matches the shared search fixture", () => {
  for (const { search, expected } of cases) {
    assert.deepEqual(parseTaskSearch(search), expected, search);
  }
});
