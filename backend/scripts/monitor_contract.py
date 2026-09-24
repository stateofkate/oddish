"""Record the /monitor wire contract that tests/test_monitor_contract.py guards.

A trial's monitor keeps the code it started with for up to 12 hours while the
API it reports to is redeployed, so /monitor formats may only grow (the rule is
in oddish/src/oddish/mon/contract.py). ``current_contract`` describes every
/monitor route field by field: its request as the API parses it, and its reply
as the API returns it. ``breaking_changes`` lists what in a recorded
description the current code no longer honours.

Record an additive change (a new optional request field, a new reply field):

    cd backend && uv run python scripts/monitor_contract.py

The script refuses a change that breaks running monitors. ``--allow-breaking``
records one anyway: use it only when no monitor that needs the old format can
still be running, i.e. monitors stopped sending or reading it in a deploy at
least 12 hours before this one.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
SNAPSHOT = BACKEND / "tests" / "fixtures" / "monitor_contract.json"
REGENERATE = "cd backend && uv run python scripts/monitor_contract.py"

# The whole request or reply; its fields are named plainly: ``events[].seq``,
# ``checkpoint.input_tokens``, ``task_files{}`` (the values of a dict).
BODY = "(body)"

# Replies a running monitor cannot take a new field in, and why.
CLOSED_REPLIES = {
    "POST /monitor/events": (
        "oddish/src/oddish/mon/seams.py builds live_tail.PersistResult(**reply), "
        "a dataclass, so a field it does not know raises TypeError in the live "
        "tail of every monitor already running"
    ),
}

BREAKING_HELP = f"""\
These /monitor changes break monitors that are already running. A monitor keeps
the code it started with for up to 12 hours, across deploys, so /monitor formats
may only grow: add a field instead of renaming one, make new request fields
optional, never narrow what a request field accepts or widen what a reply field
returns (see oddish/src/oddish/mon/contract.py). If no monitor that needs the old
format can still be running, record the change on purpose:
    {REGENERATE} --allow-breaking
"""

# Keywords that restrict what a field takes. The flattener refuses any keyword
# it does not list, rather than guess what it means for compatibility.
_LOWER_BOUNDS = ("minimum", "exclusiveMinimum", "minLength", "minItems")
_UPPER_BOUNDS = ("maximum", "exclusiveMaximum", "maxLength", "maxItems")
_EXACT = ("const", "enum", "format", "multipleOf", "pattern")
_RULES = (*_LOWER_BOUNDS, *_UPPER_BOUNDS, *_EXACT)
_KNOWN = {
    *_RULES,
    *("type", "anyOf", "$ref", "properties", "required", "items"),
    *("additionalProperties", "title", "description", "default"),
}


def current_contract() -> dict[str, Any]:
    """Every /monitor route as the current code parses and answers it."""
    from api.routers import monitor
    from fastapi.openapi.utils import get_openapi
    from harbor.trial.hooks import TrialEvent

    spec = get_openapi(title="monitor", version="0", routes=monitor.router.routes)
    routes: dict[str, Any] = {}
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            body = operation.get("requestBody")
            reply = operation["responses"].get("200", {}).get("content", {})
            reply_schema = reply.get("application/json", {}).get("schema")
            routes[f"{method.upper()} {path}"] = {
                "request": (
                    _flatten(
                        body["content"]["application/json"]["schema"],
                        spec,
                        required=body.get("required", False),
                    )
                    if body is not None
                    else None
                ),
                "reply": (
                    _flatten(reply_schema, spec, required=None)
                    if reply_schema is not None
                    else None
                ),
            }
    if (routes.get("POST /monitor/result") or {}).get("request") is not None:
        routes["POST /monitor/result"]["request"].update(_outcome_fields())
    return {
        "routes": dict(sorted(routes.items())),
        "lifecycle": {
            "events": sorted(event.value for event in TrialEvent),
            "summary": lifecycle_summary(),
        },
    }


def finished_trial_event() -> SimpleNamespace:
    """A Harbor END hook event with every part the monitor reports filled in."""
    from harbor.trial.hooks import TrialEvent

    return SimpleNamespace(
        event=TrialEvent.END,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        result=SimpleNamespace(
            verifier_result=SimpleNamespace(rewards={"reward": 1.0}),
            exception_info=SimpleNamespace(
                exception_type="AgentTimeoutError",
                exception_message="timed out",
                provider_error_code="rate_limit",
            ),
        ),
    )


def lifecycle_summary() -> dict[str, Any]:
    """The body's ``summary`` for that event. It is an open dict on the wire,
    so the test replays the recorded one through the API's parser."""
    from oddish.mon.contract import summarize_hook_event

    return summarize_hook_event(finished_trial_event())


def _outcome_fields() -> dict[str, dict]:
    """``outcome`` is an open dict on the wire, but settlement rebuilds a
    HarborOutcome from it (workers/launcher/settle.py), so those fields are
    part of the contract, minus the sandbox-local paths the monitor drops."""
    from oddish.mon.artifacts import LOCAL_ONLY_FIELDS
    from oddish.workers.harbor.outcome import HarborOutcome
    from pydantic import TypeAdapter

    schema = TypeAdapter(HarborOutcome).json_schema(mode="validation")
    for name in LOCAL_ONLY_FIELDS:
        del schema["properties"][name]
    schema["required"] = [n for n in schema["required"] if n in schema["properties"]]
    fields = _flatten(schema, schema, required=True, path="outcome")
    del fields["outcome"]  # ResultPayload's own entry says it is optional
    return fields


def _resolve(schema: dict, root: dict) -> dict:
    while "$ref" in schema:
        target = root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        schema = {**{k: v for k, v in schema.items() if k != "$ref"}, **target}
    return schema


def _flatten(
    schema: dict,
    root: dict,
    *,
    required: bool | None,
    path: str = BODY,
    into: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """One entry per field path: its JSON types, the keywords that restrict
    it and, on the request side, whether the API requires it (``required`` is
    None for replies: the API always sends every field it declares)."""
    into = {} if into is None else into
    schema = _resolve(schema, root)
    branches = [_resolve(b, root) for b in schema.get("anyOf", [schema])]
    for part in (schema, *branches):
        unknown = set(part) - _KNOWN
        if unknown:
            raise ValueError(
                f"{path}: scripts/monitor_contract.py does not know the schema "
                f"keyword(s) {sorted(unknown)}; teach it what they mean"
            )
    entry: dict[str, Any] = {"type": set()}
    for branch in branches:
        kind = branch.get("type", "any")
        entry["type"].update(kind if isinstance(kind, list) else [kind])
        for rule in _RULES:
            if rule in branch:
                if entry.get(rule, branch[rule]) != branch[rule]:
                    raise ValueError(f"{path}: union branches disagree on {rule}")
                entry[rule] = branch[rule]
    entry["type"] = sorted(entry["type"])
    if required is not None:
        entry["required"] = required
    into[path] = entry
    child = None if required is None else True
    for branch in branches:
        needed = set(branch.get("required", ()))
        for name, field in branch.get("properties", {}).items():
            _flatten(
                field,
                root,
                required=None if required is None else name in needed,
                path=name if path == BODY else f"{path}.{name}",
                into=into,
            )
        for suffix, key in (("{}", "additionalProperties"), ("[]", "items")):
            inner = branch.get(key)
            if isinstance(inner, dict) and inner:
                _flatten(inner, root, required=child, path=path + suffix, into=into)
    return into


def _parent(path: str) -> str:
    if path.endswith(("[]", "{}")):
        return path[:-2]
    return path.rpartition(".")[0] or BODY


def _gone_with_its_parent(path: str, was: dict, now: dict) -> bool:
    """A field whose parent is gone too: the parent's line reports both."""
    return _parent(path) not in now and _parent(path) in was


def _accepts(wide: dict, narrow: dict) -> bool:
    """Whether a field described by ``wide`` takes every value ``narrow`` takes."""
    types = set(wide["type"])
    for kind in narrow["type"]:
        if not ({kind, "any"} & types or (kind == "integer" and "number" in types)):
            return False
    for rule in _LOWER_BOUNDS:
        if rule in wide and not (rule in narrow and narrow[rule] >= wide[rule]):
            return False
    for rule in _UPPER_BOUNDS:
        if rule in wide and not (rule in narrow and narrow[rule] <= wide[rule]):
            return False
    for rule in _EXACT:
        if rule not in wide or wide[rule] == narrow.get(rule):
            continue
        if (
            rule == "enum"
            and "enum" in narrow
            and set(narrow["enum"]) <= set(wide["enum"])
        ):
            continue  # every value it takes is still taken
        return False
    return True


def _describe(entry: dict | None) -> str:
    if entry is None:
        return "absent"
    words = [" | ".join(entry["type"])]
    words += [f"{rule}={entry[rule]}" for rule in _RULES if rule in entry]
    if "required" in entry:
        words.append("required" if entry["required"] else "optional")
    return " ".join(words)


def _request_breaks(route: str, was: dict, now: dict) -> list[str]:
    """The API must still take everything a running monitor sends."""
    found = []
    for path, old in was.items():
        new = now.get(path)
        if new is None:
            if not _gone_with_its_parent(path, was, now):
                found.append(
                    f"{route} request: {path} is gone; running monitors still send it"
                )
            continue
        if not _accepts(new, old):
            found.append(
                f"{route} request: {path} went from {_describe(old)} to "
                f"{_describe(new)}; running monitors may send values it no longer takes"
            )
        if new.get("required") and not old.get("required"):
            found.append(
                f"{route} request: {path} became required; running monitors may "
                f"leave it out, {_refused(route, path)}"
            )
    for path, new in now.items():
        if path in was or not new.get("required"):
            continue
        if path == BODY or _parent(path) in was:
            found.append(
                f"{route} request: {path} is new and required; running monitors "
                f"do not send it, {_refused(route, path)}"
            )
    return found


def _refused(route: str, path: str) -> str:
    if route == "POST /monitor/result" and path.startswith("outcome."):
        return "so settlement cannot rebuild their HarborOutcome"
    return "so the API answers 422"


def _reply_breaks(route: str, was: dict, now: dict) -> list[str]:
    """Everything a running monitor may read must still come back, as it was."""
    found = []
    for path, old in was.items():
        new = now.get(path)
        if new is None:
            if not _gone_with_its_parent(path, was, now):
                found.append(f"{route} reply: {path} is gone; running monitors read it")
        elif not _accepts(old, new):
            found.append(
                f"{route} reply: {path} went from {_describe(old)} to "
                f"{_describe(new)}; running monitors may not read what comes back"
            )
    if route in CLOSED_REPLIES:
        for path in sorted(set(now) - set(was)):
            if _parent(path) == BODY:
                found.append(f"{route} reply: {path} is new; {CLOSED_REPLIES[route]}")
    return found


def _lifecycle_breaks(recorded: dict) -> list[str]:
    """Replay the recorded summaries, bare and full, through the API's parser."""
    from oddish.workers.launcher.hook_events import hook_event_from_summary

    failed: dict[str, list[str]] = {}
    for event in recorded["events"]:
        bare, full = {"event": event}, {**recorded["summary"], "event": event}
        for shape, summary in (("bare", bare), ("full", full)):
            try:
                hook_event_from_summary(summary)
            except Exception as exc:  # noqa: BLE001 - any failure refuses it
                reason = f"{shape} summary: {type(exc).__name__}: {exc}"
                failed.setdefault(reason, []).append(event)
    return [
        f"POST /monitor/lifecycle request: the API no longer parses a {reason} "
        f"(events {events})"
        for reason, events in failed.items()
    ]


def breaking_changes(recorded: dict, current: dict) -> list[str]:
    """What a monitor built against ``recorded`` sends or reads that the
    current code no longer honours. Empty when the change is additive."""
    found = []
    for route, was in recorded["routes"].items():
        now = current["routes"].get(route)
        if now is None:
            found.append(f"{route}: the route is gone; running monitors call it")
            continue
        found += _request_breaks(route, was["request"] or {}, now["request"] or {})
        found += _reply_breaks(route, was["reply"] or {}, now["reply"] or {})
    return found + _lifecycle_breaks(recorded["lifecycle"])


def differences(recorded: dict, current: dict) -> list[str]:
    """Every field that differs between two descriptions, one line each."""
    lines = []
    for route in sorted(set(recorded["routes"]) | set(current["routes"])):
        was = recorded["routes"].get(route) or {}
        now = current["routes"].get(route) or {}
        for side in ("request", "reply"):
            old, new = was.get(side) or {}, now.get(side) or {}
            for path in sorted(set(old) | set(new)):
                if old.get(path) != new.get(path):
                    lines.append(
                        f"{route} {side} {path}: {_describe(old.get(path))} -> "
                        f"{_describe(new.get(path))}"
                    )
    old_events = set(recorded["lifecycle"]["events"])
    new_events = set(current["lifecycle"]["events"])
    if old_events != new_events:
        added, removed = (
            sorted(new_events - old_events),
            sorted(old_events - new_events),
        )
        lines.append(
            f"POST /monitor/lifecycle summary events: added {added}, removed {removed}"
        )
    if recorded["lifecycle"]["summary"] != current["lifecycle"]["summary"]:
        lines.append("POST /monitor/lifecycle summary: what monitors send changed")
    return lines


def render(contract: dict) -> str:
    """Indented JSON with each field on one line: a change is a one-line diff."""

    def dump(value: Any, level: int) -> str:
        if level == 4 or not isinstance(value, dict) or not value:
            return json.dumps(value, sort_keys=True)
        pad = "  " * (level + 1)
        items = [
            f"{pad}{json.dumps(k)}: {dump(v, level + 1)}" for k, v in value.items()
        ]
        return "{\n" + ",\n".join(sorted(items)) + "\n" + "  " * level + "}"

    return dump(contract, 0) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--allow-breaking",
        action="store_true",
        help="record a change that breaks running monitors (read the docstring)",
    )
    args = parser.parse_args(argv)
    sys.path.insert(0, str(BACKEND))  # the API package, as pytest's pythonpath
    current = json.loads(render(current_contract()))
    changes = ["new snapshot"]
    if SNAPSHOT.exists():
        recorded = json.loads(SNAPSHOT.read_text())
        broken = breaking_changes(recorded, current)
        if broken and not args.allow_breaking:
            print(BREAKING_HELP, *(f"- {line}" for line in broken), sep="\n")
            return 1
        changes = differences(recorded, current)
    if not changes:
        print("The /monitor contract is unchanged.")
        return 0
    SNAPSHOT.write_text(render(current))
    print(f"Recorded {SNAPSHOT.relative_to(BACKEND)}:")
    print(*(f"- {line}" for line in changes), sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
