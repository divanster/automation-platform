"""Secure document storage (DOC-1): original PDF uploads, their metadata, and authorized, audited downloads. CORE, not a module.

The uploaded bytes live ONLY in a managed storage directory outside Git and outside every static or media path (``storage.py``); PostgreSQL holds the metadata and a random storage key
(``models.Document``). Nothing here extracts, parses beyond a structural envelope check, OCRs, scans or sends a document anywhere: no AI, no provider, no worker task. See
``docs/architecture/document-storage.md``.

Layers: ``rules.py`` and ``pdf.py`` (pure: names, limits, the PDF envelope), ``access.py`` (pure: who may do what), ``storage.py`` (the filesystem, standard library only, behind an injected
directory policy), ``services.py`` (authorization, the transaction, the audit event) and ``models.py``. The storage location and the platform's directory policy are chosen by the composition
root, ``config/document_storage.py``.
"""
