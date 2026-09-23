"""Default HTTP paths.

Every built-in route lives under ``/api/v1``. A router ``prefix`` is prepended,
so ``prefix="/agent"`` serves ``/agent/api/v1/...``.
"""

API_V1 = "/api/v1"

HEALTH_PATH = f"{API_V1}/health"
THREADS_PATH = f"{API_V1}/threads"
RUNS_PATH = f"{API_V1}/runs"
DEBUG_PATH = f"{API_V1}/debug"
CHANNELS_PATH = f"{API_V1}/channels"


def join_prefix(prefix: str, path: str) -> str:
    """Prepend a mount prefix to an absolute ``/api/v1/...`` path."""
    pre = prefix.strip()
    if not pre:
        return path
    if not pre.startswith("/"):
        pre = "/" + pre
    return f"{pre.rstrip('/')}{path}"
