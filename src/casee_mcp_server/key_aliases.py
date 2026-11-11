"""Environment-variable aliases for the two CaSee API key planes.

v1.8 renames the is-server retrieval key env var to ``CASEE_IS_API_KEY`` to
make the plane explicit (``CASEE_OPENAPI_API_KEY`` already names the EIRS
OpenAPI plane). The legacy ``CASEE_API_KEY`` stays supported as a fallback,
and, when ``CASEE_IS_API_KEY`` is set, it is mirrored onto ``CASEE_API_KEY``
at import time so the ``casee`` SDK — which reads ``CASEE_API_KEY`` directly
(``casee.constants.ENV_API_KEY``) — picks up the same key without any code
change on the SDK side.

Priority order for the is-server plane:
    CASEE_IS_API_KEY  >  CASEE_API_KEY (legacy)
"""
from __future__ import annotations

import os


def resolve_is_api_key() -> str:
    """Return the is-server retrieval key (new name first, legacy alias second)."""
    return (
        os.environ.get("CASEE_IS_API_KEY", "").strip()
        or os.environ.get("CASEE_API_KEY", "").strip()
    )


def apply_key_aliases() -> None:
    """Mirror ``CASEE_IS_API_KEY`` onto ``CASEE_API_KEY`` for the casee SDK.

    Only overwrites when the new variable is set, so deployments configured
    with the legacy name keep working unchanged.
    """
    is_key = os.environ.get("CASEE_IS_API_KEY", "").strip()
    if is_key:
        os.environ["CASEE_API_KEY"] = is_key
