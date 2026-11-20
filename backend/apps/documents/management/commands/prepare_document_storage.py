"""Prepare the managed document storage directory. Explicit, repeatable, never destructive.

    python manage.py prepare_document_storage

Reads ``AP_DOCUMENT_STORAGE_DIRECTORY`` (an absolute path outside the repository whose parent exists). A missing directory is created with restrictive permissions (POSIX ``0700``; Windows a protected
DACL for the current identity) and the ``incoming`` and ``objects`` folders inside it; an EXISTING directory is only verified, never changed. Run it as the account the web process serves documents
as. It prints no path.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.documents.errors import DocumentStorageError
from config.document_storage import prepare_document_storage


class Command(BaseCommand):
    help = "Create (or verify) the managed document storage directory with restrictive permissions (repeatable)."

    def handle(self, *args, **options):
        try:
            prepare_document_storage()
        except DocumentStorageError as error:
            raise CommandError(f"The document storage is not ready ({error.code}). Check AP_DOCUMENT_STORAGE_DIRECTORY, the directory's location and its permissions.") from None
        self.stdout.write("The document storage directory is ready.")
