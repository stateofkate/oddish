"""The one rich console the worker-side modules print through.

Lives outside ``workers.queue`` so the Harbor-side modules (live_tail, the
launcher) can share it without importing the queue package.
"""

from rich.console import Console

console = Console()
