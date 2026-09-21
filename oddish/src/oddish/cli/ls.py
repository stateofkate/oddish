from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import get_api_url, get_auth_headers, require_api_key
from oddish.filters.trial_metrics import TrialMetricFilter

console = Console()

# Rolling date presets resolve to lower bounds, just as in the Tasks UI.
_ROLLING_WINDOWS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


def _format_datetime(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%m-%d %H:%M")


def _format_reward(row: dict[str, Any]) -> str:
    reward_total = int(row.get("reward_total") or 0)
    if reward_total == 0:
        return "-"

    reward_success = int(row.get("reward_success") or 0)
    reward_sum = float(row.get("reward_sum") or 0)
    average = reward_sum / reward_total
    return f"{reward_success}/{reward_total} avg {average:.2f}"


def _format_trials(row: dict[str, Any]) -> str:
    total = int(row.get("total_trials") or 0)
    completed = int(row.get("completed_trials") or 0)
    failed = int(row.get("failed_trials") or 0)
    if total == 0:
        return "-"
    if failed:
        label = "fail" if failed == 1 else "fails"
        return f"{completed}/{total} ({failed} {label})"
    return f"{completed}/{total}"


def _format_experiments(row: dict[str, Any]) -> str:
    experiments = row.get("experiments") or []
    if not experiments:
        return "-"
    names = [
        experiment.get("name") or experiment.get("id") or "-"
        for experiment in experiments
    ]
    return ", ".join(names[:2]) + (" +" if len(names) > 2 else "")


def ls(
    query: Annotated[
        str | None,
        typer.Option(
            "--query",
            "-q",
            help="Filter tasks by name",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            "-n",
            min=1,
            max=100,
            help="Maximum number of tasks to show",
        ),
    ] = 25,
    offset: Annotated[
        int,
        typer.Option(
            "--offset",
            min=0,
            help="Number of tasks to skip",
        ),
    ] = 0,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit the raw JSON response",
        ),
    ] = False,
    ids_only: Annotated[
        bool,
        typer.Option(
            "--ids", help="Select all matching IDs (up to 5,000); ignores pagination."
        ),
    ] = False,
    count_only: Annotated[
        bool,
        typer.Option(
            "--count", help="Count all matching tasks without fetching task details."
        ),
    ] = False,
    filter_options: Annotated[
        bool,
        typer.Option(
            "--filter-options",
            help="List available filter values, including recorded labs and categories.",
        ),
    ] = False,
    delivery_history: Annotated[
        bool,
        typer.Option(
            "--delivery-history",
            help="Show each task's recorded lab, batch, date, and source.",
        ),
    ] = False,
    delivered_to: Annotated[
        list[str] | None,
        typer.Option("--delivered-to", help="Sent to any named lab (repeatable)."),
    ] = None,
    not_delivered_to: Annotated[
        list[str] | None,
        typer.Option(
            "--not-delivered-to",
            help="No delivery record to any named lab (repeatable).",
        ),
    ] = None,
    never_delivered: Annotated[
        bool | None,
        typer.Option(
            "--never-delivered/--has-delivery",
            help="No delivery records / at least one record; missing history is not proof of no shipment.",
        ),
    ] = None,
    category: Annotated[
        list[str] | None,
        typer.Option("--category", help="Imported task category (repeatable)."),
    ] = None,
    qa_outcome: Annotated[
        list[str] | None,
        typer.Option(
            "--qa-outcome",
            help="accepted, rejected, outdated, unreviewed, running, or failed (repeatable).",
        ),
    ] = None,
    selection_id: Annotated[str | None, typer.Option("--selection-id", help="Browse an organization-shared exact task selection.")] = None,
    share_selection: Annotated[str | None, typer.Option("--share-selection", help="Save all matching task IDs as a named organization-shared selection (hosted API; max 5000).")] = None,
    exclude_delivery_id: Annotated[
        str | None,
        typer.Option(
            "--exclude-delivery-id", help="Exclude tasks already in this delivery ID."
        ),
    ] = None,
    author: Annotated[
        list[str] | None,
        typer.Option(
            "--author",
            help="Author email, handle, user ID, or me (repeatable; hosted API).",
        ),
    ] = None,
    pin_author: Annotated[
        list[str] | None,
        typer.Option(
            "--pin-author",
            help="Put these authors first; use me for mine first (hosted API).",
        ),
    ] = None,
    steps_p50_min: Annotated[
        int | None,
        typer.Option("--steps-p50-min", min=0, help="Minimum median trial steps."),
    ] = None,
    steps_p50_max: Annotated[
        int | None,
        typer.Option("--steps-p50-max", min=0, help="Maximum median trial steps."),
    ] = None,
    agent_count_min: Annotated[
        int | None,
        typer.Option("--agent-count-min", min=1, help="Minimum distinct agents."),
    ] = None,
    trial_finished_after: Annotated[
        datetime | None, typer.Option("--trial-finished-after")
    ] = None,
    trial_finished_before: Annotated[
        datetime | None, typer.Option("--trial-finished-before")
    ] = None,
    trial_finished_within: Annotated[
        str | None,
        typer.Option(
            "--trial-finished-within",
            help="Finished-trial window: 24h, 7d, 30d, or 90d.",
        ),
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(
            "--tag",
            help="Require this tag (repeatable; AND semantics).",
        ),
    ] = None,
    tag_any: Annotated[
        list[str] | None,
        typer.Option(
            "--tag-any",
            help="Match any of these tags (repeatable; OR semantics).",
        ),
    ] = None,
    not_tag: Annotated[
        list[str] | None,
        typer.Option(
            "--not-tag",
            help="Exclude tasks that carry any of these tags (repeatable).",
        ),
    ] = None,
    # --- Task-column filters (repeatable → CSV) ---
    status: Annotated[
        list[str] | None,
        typer.Option("--status", help="Task status (repeatable)."),
    ] = None,
    priority: Annotated[
        list[str] | None,
        typer.Option("--priority", help="Task priority (repeatable)."),
    ] = None,
    verdict_status: Annotated[
        list[str] | None,
        typer.Option("--verdict-status", help="Task verdict status (repeatable)."),
    ] = None,
    experiment_id: Annotated[
        list[str] | None,
        typer.Option("--experiment-id", help="Experiment id (repeatable)."),
    ] = None,
    # --- Trial-level filters (repeatable → CSV) ---
    agent: Annotated[
        list[str] | None,
        typer.Option("--agent", help="Trial agent (repeatable)."),
    ] = None,
    model: Annotated[
        list[str] | None,
        typer.Option("--model", help="Trial model (repeatable)."),
    ] = None,
    agent_model: Annotated[
        list[str] | None,
        typer.Option(
            "--agent-model",
            help="Agent+model pair as 'agent:model' (repeatable).",
        ),
    ] = None,
    provider: Annotated[
        list[str] | None,
        typer.Option("--provider", help="Trial provider (repeatable)."),
    ] = None,
    environment: Annotated[
        list[str] | None,
        typer.Option("--environment", help="Trial environment (repeatable)."),
    ] = None,
    trial_status: Annotated[
        list[str] | None,
        typer.Option("--trial-status", help="Trial status (repeatable)."),
    ] = None,
    origin: Annotated[
        list[str] | None,
        typer.Option("--origin", help="Trial origin (repeatable)."),
    ] = None,
    analysis_classification: Annotated[
        list[str] | None,
        typer.Option(
            "--analysis-classification",
            help="Trial analysis classification (repeatable).",
        ),
    ] = None,
    harbor_sha: Annotated[
        list[str] | None,
        typer.Option("--harbor-sha", help="Harbor SHA (repeatable)."),
    ] = None,
    harbor_stage: Annotated[
        list[str] | None,
        typer.Option("--harbor-stage", help="Harbor stage (repeatable)."),
    ] = None,
    # --- Tri-state booleans (--x / --no-x; unset omits) ---
    has_link: Annotated[
        bool | None,
        typer.Option("--has-link/--no-has-link", help="Task has a source link."),
    ] = None,
    has_error: Annotated[
        bool | None,
        typer.Option("--has-error/--no-has-error", help="Trial has an error."),
    ] = None,
    has_trajectory: Annotated[
        bool | None,
        typer.Option(
            "--has-trajectory/--no-has-trajectory", help="Trial has a trajectory."
        ),
    ] = None,
    trial_is_probe: Annotated[
        bool | None,
        typer.Option(
            "--trial-is-probe/--no-trial-is-probe", help="Match probe trials."
        ),
    ] = None,
    run_analysis: Annotated[
        bool | None,
        typer.Option("--run-analysis/--no-run-analysis", help="Task runs analysis."),
    ] = None,
    run_probe: Annotated[
        bool | None,
        typer.Option("--run-probe/--no-run-probe", help="Task runs probe."),
    ] = None,
    # --- Datetimes ---
    created_after: Annotated[
        datetime | None,
        typer.Option("--created-after", help="Only tasks created after this time."),
    ] = None,
    created_before: Annotated[
        datetime | None,
        typer.Option("--created-before", help="Only tasks created before this time."),
    ] = None,
    created_within: Annotated[
        str | None,
        typer.Option(
            "--created-within",
            help="Rolling window: 24h, 7d, 30d, or 90d (resolved to created-after).",
        ),
    ] = None,
    # --- Per-trial numeric ranges ---
    min_attempts: Annotated[int | None, typer.Option("--min-attempts", min=1)] = None,
    min_tokens: Annotated[int | None, typer.Option("--min-tokens", min=0)] = None,
    max_tokens: Annotated[int | None, typer.Option("--max-tokens", min=0)] = None,
    min_steps: Annotated[int | None, typer.Option("--min-steps", min=0)] = None,
    max_steps: Annotated[int | None, typer.Option("--max-steps", min=0)] = None,
    min_duration_seconds: Annotated[
        float | None,
        typer.Option("--min-duration", min=0, help="Minimum trajectory seconds."),
    ] = None,
    max_duration_seconds: Annotated[
        float | None,
        typer.Option("--max-duration", min=0, help="Maximum trajectory seconds."),
    ] = None,
    min_tool_calls: Annotated[
        int | None, typer.Option("--min-tool-calls", min=0)
    ] = None,
    max_tool_calls: Annotated[
        int | None, typer.Option("--max-tool-calls", min=0)
    ] = None,
    tool: Annotated[
        list[str] | None,
        typer.Option("--tool", help="Require a trajectory tool name (repeatable)."),
    ] = None,
    tool_min: Annotated[
        list[str] | None,
        typer.Option(
            "--tool-min", help="Minimum per-tool count as NAME=N (repeatable)."
        ),
    ] = None,
    trial_match: Annotated[
        str,
        typer.Option("--trial-match", help="Metric match mode: any or all."),
    ] = "any",
    reward_min: Annotated[
        float | None, typer.Option("--reward-min", min=0.0, max=1.0)
    ] = None,
    reward_max: Annotated[
        float | None, typer.Option("--reward-max", min=0.0, max=1.0)
    ] = None,
    # --- Task aggregate ranges (computed on the fly) ---
    avg_score_min: Annotated[
        float | None,
        typer.Option("--avg-score-min", min=0.0, max=100.0, help="Percent 0-100."),
    ] = None,
    avg_score_max: Annotated[
        float | None,
        typer.Option("--avg-score-max", min=0.0, max=100.0, help="Percent 0-100."),
    ] = None,
    total_tokens_min: Annotated[
        int | None, typer.Option("--total-tokens-min", min=0)
    ] = None,
    total_tokens_max: Annotated[
        int | None, typer.Option("--total-tokens-max", min=0)
    ] = None,
    runtime_total_min: Annotated[
        float | None,
        typer.Option("--runtime-total-min", min=0.0, help="Seconds."),
    ] = None,
    runtime_total_max: Annotated[
        float | None,
        typer.Option("--runtime-total-max", min=0.0, help="Seconds."),
    ] = None,
    runtime_avg_min: Annotated[
        float | None,
        typer.Option("--runtime-avg-min", min=0.0, help="Seconds per trial."),
    ] = None,
    runtime_avg_max: Annotated[
        float | None,
        typer.Option("--runtime-avg-max", min=0.0, help="Seconds per trial."),
    ] = None,
    total_trials_min: Annotated[
        int | None, typer.Option("--total-trials-min", min=1)
    ] = None,
    completed_trials_min: Annotated[
        int | None, typer.Option("--completed-trials-min", min=1)
    ] = None,
    failed_trials_min: Annotated[
        int | None, typer.Option("--failed-trials-min", min=1)
    ] = None,
    pass_count_min: Annotated[
        int | None, typer.Option("--pass-count-min", min=1)
    ] = None,
    partial_count_min: Annotated[
        int | None, typer.Option("--partial-count-min", min=1)
    ] = None,
    fail_count_min: Annotated[
        int | None, typer.Option("--fail-count-min", min=1)
    ] = None,
    harness_count_min: Annotated[
        int | None, typer.Option("--harness-count-min", min=1)
    ] = None,
    pass_rate_min: Annotated[
        float | None,
        typer.Option("--pass-rate-min", min=0.0, max=100.0, help="Percent 0-100."),
    ] = None,
    pass_rate_max: Annotated[
        float | None,
        typer.Option("--pass-rate-max", min=0.0, max=100.0, help="Percent 0-100."),
    ] = None,
    # --- Aggregate sort ---
    sort: Annotated[
        str | None,
        typer.Option(
            "--sort",
            help=(
                "avg_score_(asc|desc), total_tokens_(asc|desc), "
                "runtime_total_(asc|desc), runtime_avg_(asc|desc), cost_desc, "
                "steps_p50_(asc|desc), total_trials_(asc|desc), agent_count_(asc|desc); "
                "omit for recent activity."
            ),
        ),
    ] = None,
    # --- Agent/model comparison (A beats B on a metric) ---
    compare_by: Annotated[
        str | None,
        typer.Option("--compare-by", help="Compare subject: agent or model."),
    ] = None,
    compare_a: Annotated[
        str | None, typer.Option("--compare-a", help="Subject A name.")
    ] = None,
    compare_b: Annotated[
        str | None, typer.Option("--compare-b", help="Subject B name.")
    ] = None,
    compare_metric: Annotated[
        str | None,
        typer.Option(
            "--compare-metric",
            help="reward | runtime | tokens | steps | pass_rate.",
        ),
    ] = None,
    compare_agg: Annotated[
        str | None,
        typer.Option("--compare-agg", help="best | avg | median."),
    ] = None,
    compare_margin: Annotated[
        float | None, typer.Option("--compare-margin", min=0.0)
    ] = None,
    compare_margin_unit: Annotated[
        str | None,
        typer.Option("--compare-margin-unit", help="pct (default) or abs."),
    ] = None,
    # --- Top performer (best subject per task) ---
    top_by: Annotated[
        str | None,
        typer.Option("--top-by", help="Top performer subject: agent or model."),
    ] = None,
    top_value: Annotated[
        str | None,
        typer.Option("--top-value", help="Subject that must be the top performer."),
    ] = None,
    top_metric: Annotated[
        str | None,
        typer.Option(
            "--top-metric",
            help="reward | runtime | tokens | steps | pass_rate.",
        ),
    ] = None,
    # --- OR-groups ("Match any of…") ---
    or_groups: Annotated[
        str | None,
        typer.Option(
            "--or-groups",
            help="Raw JSON list of condition dicts (same keys as the flat params).",
        ),
    ] = None,
    api_url: Annotated[
        str,
        typer.Option("--api", help="API URL"),
    ] = "",
) -> None:
    """List uploaded tasks."""
    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    params: dict[str, Any] = {"limit": limit, "offset": offset}

    tool_count_mins: dict[str, int] = {}
    for item in tool_min or []:
        name, separator, raw_count = item.partition("=")
        if not separator or not name.strip():
            raise typer.BadParameter("must use NAME=N", param_hint="--tool-min")
        try:
            tool_count_mins[name.strip()] = int(raw_count)
        except ValueError as exc:
            raise typer.BadParameter(
                "count must be an integer", param_hint="--tool-min"
            ) from exc
    try:
        metric_filter = TrialMetricFilter.from_query(
            models=model,
            min_steps=min_steps,
            max_steps=max_steps,
            min_duration_seconds=min_duration_seconds,
            max_duration_seconds=max_duration_seconds,
            min_tool_calls=min_tool_calls,
            max_tool_calls=max_tool_calls,
            tool_names=tool,
            tool_count_mins=tool_count_mins,
            match=trial_match,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="trial metric filters") from exc
    params.update(metric_filter.to_query_params())

    def _csv(name: str, values: list[str] | None) -> None:
        if values:
            params[name] = ",".join(values)

    def _bool(name: str, value: bool | None) -> None:
        if value is not None:
            params[name] = "true" if value else "false"

    def _num(name: str, value: int | float | None) -> None:
        if value is not None:
            params[name] = value

    def _str(name: str, value: str | None) -> None:
        if value:
            params[name] = value

    if query:
        params["query"] = query
    _csv("delivered_to", delivered_to)
    _csv("not_delivered_to", not_delivered_to)
    _csv("categories", category)
    _csv("qa_outcomes", qa_outcome)
    _csv("author", author)
    _csv("pin_author", pin_author)
    _str("exclude_delivery_id", exclude_delivery_id)
    _str("selection_id", selection_id)
    _bool("never_delivered", never_delivered)
    _num("steps_p50_min", steps_p50_min)
    _num("steps_p50_max", steps_p50_max)
    _num("agent_count_min", agent_count_min)
    if trial_finished_before:
        params["trial_finished_before"] = trial_finished_before.isoformat()
    _csv("tags", tag)
    _csv("tags_any", tag_any)
    _csv("tags_none", not_tag)
    _csv("statuses", status)
    _csv("priorities", priority)
    _csv("verdict_statuses", verdict_status)
    _csv("experiment_ids", experiment_id)
    _csv("agents", agent)
    _csv("agent_models", agent_model)
    _csv("providers", provider)
    _csv("environments", environment)
    _csv("trial_statuses", trial_status)
    _csv("origins", origin)
    _csv("analysis_classifications", analysis_classification)
    _csv("harbor_shas", harbor_sha)
    _csv("harbor_stages", harbor_stage)
    _bool("has_link", has_link)
    _bool("has_error", has_error)
    _bool("has_trajectory", has_trajectory)
    _bool("trial_is_probe", trial_is_probe)
    _bool("run_analysis", run_analysis)
    _bool("run_probe", run_probe)
    if created_before:
        params["created_before"] = created_before.isoformat()
    # Rolling presets take precedence over explicit lower bounds, as in the UI.
    for prefix, window, after in (
        ("created", created_within, created_after),
        ("trial_finished", trial_finished_within, trial_finished_after),
    ):
        if window is not None:
            if window not in _ROLLING_WINDOWS:
                raise typer.BadParameter(
                    "must be one of: " + ", ".join(_ROLLING_WINDOWS),
                    param_hint=f"--{prefix.replace('_', '-')}-within",
                )
            after = datetime.now(timezone.utc) - _ROLLING_WINDOWS[window]
        if after:
            params[f"{prefix}_after"] = after.isoformat()
    _num("min_attempts", min_attempts)
    _num("min_tokens", min_tokens)
    _num("max_tokens", max_tokens)
    _num("reward_min", reward_min)
    _num("reward_max", reward_max)
    _num("avg_score_min", avg_score_min)
    _num("avg_score_max", avg_score_max)
    _num("total_tokens_min", total_tokens_min)
    _num("total_tokens_max", total_tokens_max)
    _num("runtime_total_min", runtime_total_min)
    _num("runtime_total_max", runtime_total_max)
    _num("runtime_avg_min", runtime_avg_min)
    _num("runtime_avg_max", runtime_avg_max)
    _num("total_trials_min", total_trials_min)
    _num("completed_trials_min", completed_trials_min)
    _num("failed_trials_min", failed_trials_min)
    _num("pass_count_min", pass_count_min)
    _num("partial_count_min", partial_count_min)
    _num("fail_count_min", fail_count_min)
    _num("harness_count_min", harness_count_min)
    _num("pass_rate_min", pass_rate_min)
    _num("pass_rate_max", pass_rate_max)
    _str("sort", sort)
    # Compare — mirror the UI's filterParams(): only emit as a complete,
    # defaulted group when a distinct A/B pair is given. The backend skips the
    # comparison unless compare_by + compare_metric + compare_a + compare_b are
    # all present (it defaults compare_agg itself), so forwarding partial flags
    # would silently return unfiltered results.
    if compare_a and compare_b and compare_a != compare_b:
        params["compare_by"] = compare_by or "agent"
        params["compare_a"] = compare_a
        params["compare_b"] = compare_b
        params["compare_metric"] = compare_metric or "reward"
        params["compare_agg"] = compare_agg or "best"
        if compare_margin is not None:
            params["compare_margin"] = compare_margin
            params["compare_margin_unit"] = compare_margin_unit or "pct"
    # Top performer — needs a subject value; default by/metric (mirrors the UI).
    if top_value:
        params["top_by"] = top_by or "agent"
        params["top_value"] = top_value
        params["top_metric"] = top_metric or "reward"
    _str("or_groups", or_groups)

    if share_selection is not None and not share_selection.strip():
        raise typer.BadParameter("selection name cannot be blank", param_hint="--share-selection")
    if share_selection and (ids_only or count_only or filter_options or delivery_history):
        raise typer.BadParameter("--share-selection cannot be combined with other output modes")
    if sum((ids_only, count_only, filter_options)) > 1:
        raise typer.BadParameter(
            "--ids, --count, and --filter-options are mutually exclusive"
        )
    if delivery_history and (ids_only or count_only or filter_options):
        raise typer.BadParameter("--delivery-history requires task rows")
    if filter_options and params != {"limit": limit, "offset": offset}:
        raise typer.BadParameter(
            "--filter-options cannot be combined with task filters"
        )
    if ids_only or share_selection:
        params["ids_only"] = "true"
    if count_only:
        params["count_only"] = "true"
    endpoint = "tasks/browse/facets" if filter_options else "tasks/browse"
    try:
        with httpx.Client(timeout=30.0, headers=get_auth_headers(api_url)) as client:
            response = client.get(
                f"{api_url}/{endpoint}", params={} if filter_options else params
            )
    except httpx.HTTPError as exc:
        console.print(f"[red]Failed to connect to API:[/red] {exc}")
        raise typer.Exit(1) from exc

    if response.status_code != 200:
        console.print(f"[red]Failed to list tasks:[/red] {response.text}")
        raise typer.Exit(1)

    result = response.json()
    if share_selection:
        if result.get("truncated") is not False or not result.get("ids"):
            console.print("[red]Selection must contain 1–5000 tasks. Narrow the filters if truncated.[/red]")
            raise typer.Exit(1)
        try:
            with httpx.Client(timeout=30.0, headers=get_auth_headers(api_url)) as client:
                saved = client.post(f"{api_url}/tag-filters", json={
                    "name": share_selection.strip(), "visibility": "ORG",
                    "filter_ast": {"v": 2, "task_ids": result["ids"]},
                })
        except httpx.HTTPError as exc:
            console.print(f"[red]Could not share selection:[/red] {exc}")
            raise typer.Exit(1) from exc
        if saved.status_code not in (200, 201):
            console.print(f"[red]Could not share selection:[/red] {saved.text}")
            raise typer.Exit(1)
        result = saved.json()
        if json_output:
            print(json.dumps(result, indent=2))
        else:
            console.print(f"Saved {len(result['filter_ast']['task_ids'])} tasks. Open with: oddish ls --selection-id {result['id']}", markup=False)
        return
    if json_output:
        print(json.dumps(result, indent=2))
        return
    if ids_only:
        if result["truncated"]:
            console.print(
                "[red]Selection exceeds 5,000 tasks; narrow the filters or use --json to inspect truncation.[/red]",
                highlight=False,
            )
            raise typer.Exit(1)
        print("\n".join(result["ids"]))
        return
    if count_only:
        print(result["total"])
        return
    if filter_options:
        table = Table("Filter", "Values")
        for key, values in result.items():
            if values:
                table.add_row(
                    key,
                    ", ".join(
                        f"{value['agent']}:{value['model'] or ''}"
                        if isinstance(value, dict)
                        else value
                        for value in values
                    ),
                )
        console.print(table)
        return

    tasks = result.get("items") or []
    if not tasks:
        console.print("[dim]No tasks found[/dim]")
        return

    table = Table(title="Tasks", show_header=True)
    table.add_column("Task", style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Ver", justify="right", no_wrap=True)
    table.add_column("Trials", justify="right", no_wrap=True)
    table.add_column("Reward", justify="right", no_wrap=True)
    table.add_column("Last", no_wrap=True)
    table.add_column("Exp")
    table.add_column("Tags")
    table.add_column("Sent to")

    for task in tasks:
        current_version = task.get("current_version")
        version = f"v{current_version}" if current_version is not None else "-"
        user_tags = task.get("user_tags") or []
        primary = [t["key"] for t in user_tags if t.get("current")]
        older_only = [
            t["key"] for t in user_tags if t.get("older") and not t.get("current")
        ]
        tag_label = ", ".join(primary)
        if older_only:
            older_str = "(" + ", ".join(older_only) + ")"
            tag_label = f"{tag_label} {older_str}" if tag_label else older_str
        table.add_row(
            task.get("id", "-"),
            task.get("name") or "-",
            version,
            _format_trials(task),
            _format_reward(task),
            _format_datetime(task.get("last_run_at")),
            _format_experiments(task),
            tag_label or "-",
            ", ".join(dict.fromkeys(d["customer"] for d in task.get("deliveries", [])))
            or "No record",
        )

    console.print(table)
    if delivery_history:
        history = Table("Task", "Lab", "Batch", "Date", "Source")
        for task in tasks:
            for record in task.get("deliveries", []):
                history.add_row(
                    task["id"],
                    record["customer"],
                    record.get("batch") or "-",
                    record.get("date") or "-",
                    record["source"],
                )
        console.print(history)
    if result.get("has_more"):
        next_offset = offset + limit
        console.print(f"[dim]More available: oddish ls --offset {next_offset}[/dim]")
