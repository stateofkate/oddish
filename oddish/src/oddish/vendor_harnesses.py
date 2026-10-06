"""Agent harnesses that send every model through their vendor's own service.

Cursor CLI and Devin CLI pick the model on their vendor's side: the sandbox
only ever talks to that vendor's API, authenticates with one vendor key
whatever model runs, and needs no provider credential of its own. Each entry
here is the single source for that harness's key names, model prefix, and
runtime egress hosts; the worker credential fold, redaction map, key
fingerprinting, and host allowlists all read it instead of naming the harness.

Dependency-free so ``oddish.core`` and ``oddish.workers`` can both import it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VendorHarness:
    # Harbor agent name, e.g. ``--agent devin``.
    agent_name: str
    # Harbor/Oddish module basenames implementing the harness. A trial pinned
    # by ``import_path`` matches on the ``<module>:`` class-path boundary.
    modules: tuple[str, ...]
    # ``<prefix>/<model>`` ids select this vendor's service (``devin/swe-2``).
    model_prefix: str
    # Worker env names the harness authenticates with, forwarded from the
    # worker environment and folded into artifact redaction.
    auth_env_keys: tuple[str, ...]
    # Hosts the harness dials at runtime. Empty means not yet established, and
    # restricted-network trials fail closed for this harness.
    runtime_hosts: tuple[str, ...]
    # Hosts its self-install needs during agent SETUP. A closed task runs setup
    # under the environment baseline, so these ride along with runtime_hosts.
    install_hosts: tuple[str, ...] = ()
    # Bare model ids are rejected at submit: without the prefix, Oddish's
    # provider routing would rewrite the id for a provider the harness never
    # calls (e.g. ``claude-*`` -> ``anthropic/`` or ``bedrock/``).
    requires_model_prefix: bool = False

    def matches(self, name: str | None, import_path: str | None = None) -> bool:
        """Whether an agent name or import path selects this harness."""
        normalized_name = (name or "").strip().lower()
        normalized_path = (import_path or "").strip().lower()
        return normalized_name == self.agent_name or any(
            f"{module}:" in normalized_path for module in self.modules
        )

    def has_model_prefix(self, model: str | None) -> bool:
        head, separator, rest = (model or "").strip().lower().partition("/")
        return bool(separator and rest) and head == self.model_prefix


CURSOR_CLI = VendorHarness(
    agent_name="cursor-cli",
    modules=("cursor_cli",),
    model_prefix="cursor",
    auth_env_keys=("CURSOR_API_KEY",),
    # Cursor's bootstrap endpoint returns the agent-stream URL at runtime
    # (currently under api5), and the installer is intentionally unpinned. Use
    # Cursor's official domain boundary instead of ephemeral transport hosts.
    runtime_hosts=("*.cursor.sh",),
)

DEVIN = VendorHarness(
    agent_name="devin",
    modules=("devin",),
    model_prefix="devin",
    # Harbor's Devin agent writes this key into the CLI's credentials.toml and
    # keeps it out of the agent's run environment.
    auth_env_keys=("DEVIN_API_KEY",),
    # Connections in the CLI's RUST_LOG=debug logs from devin/swe-2 runs on
    # staging (Devin CLI 3000.11.3): login and inference, remote config, and
    # static assets. Its Sentry ingest host is left out (error reporting only).
    runtime_hosts=("server.codeium.com", "api.devin.ai", "static.devin.ai"),
    install_hosts=(
        "cli.devin.ai",  # install.sh
        "static.devin.ai",  # CLI binary (install.sh downloads static.devin.ai/cli)
        # ``install`` first runs ``apt-get install curl``; slim images without
        # curl need their distro mirrors.
        "deb.debian.org",
        "archive.ubuntu.com",
        "security.ubuntu.com",
    ),
    requires_model_prefix=True,
)

VENDOR_HARNESSES: tuple[VendorHarness, ...] = (CURSOR_CLI, DEVIN)


def vendor_harness_for_agent(
    name: str | None, import_path: str | None = None
) -> VendorHarness | None:
    for harness in VENDOR_HARNESSES:
        if harness.matches(name, import_path):
            return harness
    return None


def vendor_harness_for_model_prefix(prefix: str | None) -> VendorHarness | None:
    normalized = (prefix or "").strip().lower()
    for harness in VENDOR_HARNESSES:
        if harness.model_prefix == normalized:
            return harness
    return None


def vendor_model_prefix_error(agent: str | None, model: str | None) -> str | None:
    """Why ``model`` cannot run on ``agent``'s vendor harness, else None."""
    harness = vendor_harness_for_agent(agent)
    if harness is None or not harness.requires_model_prefix:
        return None
    if harness.has_model_prefix(model):
        return None
    return (
        f"{harness.agent_name} models need the {harness.model_prefix}/ prefix "
        f"(e.g. {harness.model_prefix}/swe-2); got {model!r}."
    )
