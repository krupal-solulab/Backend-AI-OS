"""Documents (Phase 1) — store/retrieve raw docs + metadata.

Metadata lives in the ``Document`` table; bytes live on local disk under
``DOCUMENT_STORAGE_ROOT`` (tenant/submission-scoped paths). The ``DocumentStore``
interface is storage-backend agnostic, so the local backend is swappable for S3 later
without touching callers.
"""

from core.documents.store import DocumentStore, LocalDocumentStore

__all__ = ["DocumentStore", "LocalDocumentStore"]
