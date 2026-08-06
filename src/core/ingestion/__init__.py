"""Ingestion — the shared ``ConnectorService`` (Nango-shaped) with a fixtures-backed
mock and a real Nango-proxy-backed live implementation, selected by
``CONNECTORS_MODE``.

Hard rules (CONNECTORS_NANGO.md): no direct Google SDKs (Gmail is only ever reached
through the Nango proxy); tenant-scoped; and **no auto-send** — ``send_email`` is
only ever invoked from a human-triggered action, never from the ingestion path.
"""

from core.ingestion.connectors import (
    ConnectorNotConnectedError,
    ConnectorService,
    EmailMessage,
    FileBlob,
    LiveNangoConnectorService,
    MockConnectorService,
    SentRef,
    build_connector_service,
)

__all__ = [
    "ConnectorNotConnectedError",
    "ConnectorService",
    "EmailMessage",
    "FileBlob",
    "LiveNangoConnectorService",
    "MockConnectorService",
    "SentRef",
    "build_connector_service",
]
