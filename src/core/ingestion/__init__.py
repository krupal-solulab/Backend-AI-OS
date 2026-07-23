"""Ingestion (Phase 1) — the shared ``ConnectorService`` (Nango-shaped) with a
fixtures-backed mock and a live REST stub, selected by ``CONNECTORS_MODE``.

Hard rules (CONNECTORS_NANGO.md): no direct Google SDKs; tenant-scoped; and **no
auto-send** — ``send_email`` is only ever invoked from a human-triggered action, never
from the ingestion path. In Phase 1 the live path makes **no real network calls**.
"""

from core.ingestion.connectors import (
    ConnectorService,
    EmailMessage,
    FileBlob,
    LiveNangoConnectorService,
    MockConnectorService,
    SentRef,
    build_connector_service,
)

__all__ = [
    "ConnectorService",
    "EmailMessage",
    "FileBlob",
    "LiveNangoConnectorService",
    "MockConnectorService",
    "SentRef",
    "build_connector_service",
]
