"""Shared pieces of ``oddish qa evals`` and ``oddish qa goldens``: the API
client, links to the QA Evals page (the admin panel's QA Evals tab), output,
option types, and the page's experiment-search grammar."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlencode

import httpx
import typer

from oddish.analyze.qa_stages import QAStage
from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    get_dashboard_url,
    print_json,
)

# The same Literals as oddish.schemas (a test keeps them equal); importing
# the schemas would double the CLI's start-up time.
QAEvalGroup = Literal["wrong", "right", "not_judged", "no_golden"]
QAGoldenState = Literal["counted", "waiting", "stale"]
QAGoldenReview = Literal["waiting", "stale"]
QAGoldenQueueGroup = Literal["failed", "passed", "labeled"]

ApiOption = Annotated[str | None, typer.Option("--api", help="Override the API URL.")]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Print the API's JSON instead.")
]
StageOption = Annotated[
    QAStage,
    typer.Option(help="post_trial (trial classification) or pre_trial (task audit)."),
]
GroupOption = Annotated[
    QAEvalGroup | None, typer.Option("--filter", help="Only cases in this group.")
]

# The page's URL keys in link order, with the defaults its links leave out
# (``KEYS`` in frontend/src/lib/qa-evals.ts).
_PAGE_KEYS: dict[str, str | None] = {
    "stage": QAStage.POST_TRIAL.value,
    "chart": "history",
    "run": None,
    "filter": "all",
    "version": None,
    "base_version": None,
    "trial": None,
    "hide": None,
    "label": None,
    "experiment": None,
    "review": None,
    "member": "me",
    "search": None,
    "queue": "failed",
}


class QAClient:
    def __init__(self, client: httpx.Client, api_url: str):
        self.client = client
        self.dashboard_url = get_dashboard_url(api_url)

    def send(self, method: str, path: str, **kwargs: Any) -> tuple[Any, str | None]:
        """The decoded body (None for 204), or None and the API's error as
        "status detail"."""
        response = self.client.request(method, path, **kwargs)
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            return None, f"{response.status_code} {_detail_text(detail)}"
        return (None if response.status_code == 204 else response.json()), None

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        """``send``, exiting 1 with the API's error."""
        body, error = self.send(method, path, **kwargs)
        if error:
            typer.echo(f"{method} {path}: {error}", err=True)
            raise typer.Exit(1)
        return body

    def page_url(self, **keys: str | list[str] | None) -> str:
        """The QA Evals page showing ``keys``; defaults are left out, and
        ``label`` is implied by ``experiment`` or ``review``."""
        if keys.get("experiment") or keys.get("review"):
            keys.pop("label", None)
        params = [("tab", "qa-evals")]
        for key, default in _PAGE_KEYS.items():
            value = keys.pop(key, None)
            for item in value if isinstance(value, list) else [value]:
                item = getattr(item, "value", item)
                if item not in (None, "", default):
                    params.append((key, item))
        assert not keys, f"not page keys: {sorted(keys)}"
        return f"{self.dashboard_url}/admin?{urlencode(params)}"

    def trial_url(self, task_id: str, trial_id: str) -> str:
        """The trial's drawer on its task page (``trialHref`` on the page)."""
        query = urlencode({"trial": trial_id})
        return f"{self.dashboard_url}/tasks/{quote(task_id, safe='')}?{query}"


def _detail_text(detail: Any) -> str:
    """A request-validation 422 (a list of pydantic errors) as "field: message"
    lines joined with "; "; any other detail as is."""
    if not isinstance(detail, list):
        return str(detail)
    return "; ".join(
        f"{'.'.join(map(str, e['loc'][1:])) or e['loc'][0]}: {e['msg']}"
        if isinstance(e, dict) and e.get("loc")
        else str(e)
        for e in detail
    )


@contextmanager
def qa_client(api: str | None) -> Iterator[QAClient]:
    api_url = (api or get_api_url()).rstrip("/")
    with httpx.Client(
        base_url=api_url, headers=get_auth_headers(api_url), timeout=60.0
    ) as client:
        yield QAClient(client, api_url)


def emit(json_output: bool, data: Any, lines: Iterable[str], url: str) -> None:
    """``data`` as JSON, or ``lines`` then the page link."""
    if json_output:
        print_json(data)
        return
    for line in lines:
        typer.echo(line)
    typer.echo(url)


@dataclass(slots=True)
class TaskSearch:
    text: str = ""
    all: list[str] = field(default_factory=list)
    any: list[str] = field(default_factory=list)
    none: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)


# A quoted phrase (optionally negated, unterminated runs to the end) or a word.
_TOKEN = re.compile(r'-?"[^"]*(?:"|\Z)\S*|\S+')
_AUTHOR = re.compile(r"(?:github|author|user):(.+)", re.IGNORECASE)
_TAG = re.compile(r"(-?)tag:(.+)", re.IGNORECASE)


def parse_task_search(raw: str) -> TaskSearch:
    """``parseTaskSearch`` in frontend/src/lib/tag-query.ts, so a search
    lists the same experiments here as on the page (a shared fixture keeps
    the two equal): ``tag:``/``-tag:`` words, ``AND``/``OR``/``NOT`` before a
    tag, ``author:`` words, and the rest as free text."""
    tokens = _TOKEN.findall(raw)
    search = TaskSearch()
    text: list[str] = []
    pending: str | None = None  # OR or NOT, waiting for the next tag
    # The previous tag, when it was just put in ``all``: OR moves it to ``any``.
    previous_in_all = False
    for i, token in enumerate(tokens):
        if tag := _TAG.fullmatch(token):
            negated, name = tag.group(1) == "-", tag.group(2)
            if negated or pending == "NOT":
                search.none.append(name)
            elif pending == "OR":
                if previous_in_all:
                    search.any.append(search.all.pop())
                search.any.append(name)
            else:
                search.all.append(name)
            previous_in_all = not negated and pending is None
            pending = None
            continue
        op = token.upper()
        following = tokens[i + 1] if i + 1 < len(tokens) else ""
        if op in ("AND", "OR", "NOT") and _TAG.fullmatch(following):
            pending = None if op == "AND" else op
            continue
        pending, previous_in_all = None, False
        if author := _AUTHOR.fullmatch(token):
            search.authors.append(author.group(1))
        else:
            text.append(token)
    search.text = " ".join(text)
    return search
