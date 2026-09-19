"""Job-board provider modules for the Veto MCP server.

Each module exposes a provider class with:

    name                       # board token used as the job-id prefix
    search(query, location, limit, remote_only) -> list[dict]
    get_details(payload) -> dict

Job ids are self-describing: ``<board>:<base64url(payload)>`` where the
payload is a board-specific locator (e.g. ``"stripe:12345"``). Decode with
``_common.decode_payload(job_id.partition(":")[2])`` to recover the payload,
then ``payload.partition(":")`` to split it back into its parts.

IMPORTANT: these modules must NOT import ``server.py`` (``server.py``
imports them; the reverse would be a circular import). Shared helpers live
in ``_common.py``.
"""

from providers.ashby import AshbyProvider
from providers.adzuna import AdzunaProvider
from providers.greenhouse import GreenhouseProvider
from providers.lever import LeverProvider

__all__ = [
    "GreenhouseProvider",
    "LeverProvider",
    "AshbyProvider",
    "AdzunaProvider",
]
