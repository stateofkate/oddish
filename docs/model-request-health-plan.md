# Model request health: follow-up scope

This is a proposal for ABT-1158's original request: show whether actual model
requests are failing, including rate limits, before users submit more jobs.
It is separate from the scheduled endpoint checker, which sends a short test
prompt using a platform credential. No Airbrake integration is proposed.

## What exists and what is missing

`backend/endpoint_health.py` records scheduled test requests in
`endpoint_checks`. These observations describe the tested connection, not the
failure rate of trial traffic. A passing check does not measure capacity under
load, agent startup, tools, or long-context behavior.

`oddish/src/oddish/observability.py` emits `trial_execution_finished`, one
structured event after a trial settlement commits, with the model, execution
status, exception type, and provider error code when available.
`oddish/src/oddish/workers/agents/provider_errors.py` classifies recognizable
provider failures in agent CLI output, including authentication, missing models,
rate limits, timeouts, and server failures. This is run-level evidence: one trial
can contain many model calls, including failed calls followed by successful
retries. Infrastructure failures must not be counted as model failures.

The October 5 production Logfire audit found 13,241 matching trial-completion
records in its last-day window and no records with `gen_ai.request.model`, the
standard model-request attribute. This query does not prove that no other
request evidence exists. It establishes that a complete per-request denominator
has not yet been verified. Count neither log lines nor trial completions as
individual model requests. Audit the runner-specific request/trajectory data
before committing to historical request error rates.

## Display

Keep a compact **Connection checks** summary with the last successful test and
current connection errors. Add a separate **Model request reliability** table
with model, provider/connection, total observed requests, failed requests,
failure percentage, rate-limit count, and last observed request. Show the exact
count beside each percentage, a 24-hour default window, and 1-hour/7-day choices.
An expanded row shows a five-minute trend and error categories; links open a
representative authorized trial or incident. Avoid making the Admin overview a
wall of healthy connection cards.

The request table must distinguish platform credentials from customer-owned
connections and respect organization visibility. One account's model-access
failure does not make the model globally unavailable. Group by the actual
provider route and a nonsecret connection identifier, not model name alone.
Keep “No traffic,” “Collection incomplete,” and “No failures observed” distinct.
Show low sample counts rather than assigning a confident healthy badge.

Each HTTP attempt, including retries, contributes once to the request denominator.
Failed attempts divided by all attempts is the displayed request error rate.
Show recovered retries separately so an eventually successful run does not erase
rate-limit pressure. Timeouts and network failures without an HTTP response have
explicit categories. Successful streaming requests finish only when the stream
ends successfully; a stream that starts and then fails is an error. Do not count
manual probes, task commands, sandbox errors, or user cancellations as provider
request failures.

## Delivery stages

1. Finish endpoint monitoring first: derive targets from existing configured
   model limits and runtime credentials, preserve explicit overrides, verify
   provider requests, and record scheduled observations after deployment.
2. Audit request coverage per runner. For SDK-based agents, identify one hook
   around the actual model call and streaming completion. For external CLIs,
   verify whether their machine-readable events contain every attempt and
   outcome; generic terminal output is insufficient. Publish a coverage matrix
   before choosing a first supported set of runners.
3. Emit a small completion event with stable attempt ID, timestamp, trial/run
   attempt ID, organization, requested/resolved model, route, nonsecret
   connection ID, outcome/category, HTTP status when present, latency, and retry
   linkage. Never collect prompt/response content or credentials for this view.
   Deduplicate delivery using the stable attempt ID.
4. Aggregate into five-minute buckets outside page requests. Add a bounded,
   organization-authorized read API and the table above. Preserve the distinction
   between covered and uncovered runners; do not extrapolate an organization-wide
   percentage from partial collection. Retain enough recent sanitized examples
   to link an error category to a real failing request.

If an earlier interim view is useful, label it **Runs affected by provider
errors** and use classified terminal run outcomes. It must not be called a model
request error rate. Automatic queue pauses, provider failover, customer-key test
requests, and importing old events into request totals remain outside this scope.

## Acceptance evidence

A fixture containing successful calls, failed attempts, recovered retries, and a
mid-stream failure produces the exact expected numerator and denominator without
double-counting. Authentication and model-access errors remain isolated to their
connection. An infrastructure-only failed trial contributes zero model errors.
An unsupported runner shows incomplete coverage instead of 0%. Cross-organization
reads and error links obey existing authorization. A page read uses precomputed
buckets and never scans all trials or calls Logfire for every page refresh.
