import assert from "node:assert/strict";
import test from "node:test";

import {
  buildDashboardApiPath,
  dashboardExperimentsRequest,
  queueExperimentsHref,
} from "../src/lib/dashboard-request.ts";

const apiParams = (href: string) =>
  new URL(
    buildDashboardApiPath(
      dashboardExperimentsRequest(new URL(href, "https://x").searchParams)
    ),
    "https://x"
  ).searchParams;

test("a Queue Health link reaches the API as the queue filter", () => {
  const href = queueExperimentsHref("anthropic/claude-opus-4-5", "queued");
  assert.equal(
    href,
    "/dashboard?queue=anthropic%2Fclaude-opus-4-5&queue_state=queued"
  );
  const params = apiParams(href);
  assert.equal(params.get("experiments_queue"), "anthropic/claude-opus-4-5");
  assert.equal(params.get("experiments_queue_state"), "queued");
});

test("queue_state needs a queue and a known value", () => {
  assert.equal(
    apiParams("/dashboard?queue_state=queued").get("experiments_queue_state"),
    null
  );
  assert.equal(
    apiParams("/dashboard?queue=q&queue_state=bogus").get(
      "experiments_queue_state"
    ),
    null
  );
});
