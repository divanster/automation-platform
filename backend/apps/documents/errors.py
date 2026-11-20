"""Errors of the document slice. Pure: no Django import. A message never holds a filename, a path, a hash or any content; ``reason`` and ``code`` are closed vocabularies."""

REASON_INVALID_FILENAME = "invalid_filename"
REASON_UNSUPPORTED_TYPE = "unsupported_type"
REASON_LENGTH_REQUIRED = "length_required"
REASON_EMPTY = "empty"
REASON_TOO_LARGE = "too_large"
REASON_LENGTH_MISMATCH = "length_mismatch"
REASON_NOT_A_PDF = "not_a_pdf"
REASON_TRUNCATED_PDF = "truncated_pdf"
REASON_MALFORMED_PDF = "malformed_pdf"
REJECTION_REASONS = (
    REASON_INVALID_FILENAME,
    REASON_UNSUPPORTED_TYPE,
    REASON_LENGTH_REQUIRED,
    REASON_EMPTY,
    REASON_TOO_LARGE,
    REASON_LENGTH_MISMATCH,
    REASON_NOT_A_PDF,
    REASON_TRUNCATED_PDF,
    REASON_MALFORMED_PDF,
)

CODE_DISABLED = "document_storage_disabled"  # no storage directory is configured: every operation fails closed
CODE_UNAVAILABLE = "document_storage_unavailable"  # the directory is unsafe, missing or unusable, or an I/O error occurred
CODE_OBJECT_MISSING = "document_object_missing"  # the metadata exists but the stored object is missing or does not match it
STORAGE_CODES = (CODE_DISABLED, CODE_UNAVAILABLE, CODE_OBJECT_MISSING)


class DocumentRejectedError(ValueError):
    """The submitted document or its description is not acceptable. ``reason`` is one of ``REJECTION_REASONS``; nothing was stored."""

    def __init__(self, reason: str):
        if reason not in REJECTION_REASONS:
            raise ValueError("unknown rejection reason")
        self.reason = reason
        super().__init__(f"The document was rejected ({reason}).")


class DocumentNotFoundError(LookupError):
    """No document has this identifier."""

    def __init__(self):
        super().__init__("The document does not exist.")


class DocumentStorageError(Exception):
    """The managed storage cannot be used. ``code`` is one of ``STORAGE_CODES``; the message is fixed and holds no path."""

    def __init__(self, code: str):
        if code not in STORAGE_CODES:
            code = CODE_UNAVAILABLE
        self.code = code
        super().__init__(f"The document storage cannot be used ({code}).")


class DocumentImmutableError(Exception):
    """An attempt to change or delete a stored document's metadata (only the processing state may change)."""
