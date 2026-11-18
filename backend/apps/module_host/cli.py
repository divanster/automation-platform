"""Shared helpers of the administrator commands ``module_sync``, ``module_status``, ``module_enable`` and ``module_disable``.

The commands read the approved static registry (each command imports it from the composition root, ``config.modules``) and call ``apps.module_host.services``; they import no module,
run no migration, install nothing and execute nothing they were not written to do. Output carries ids, versions, flags and a fingerprint, never a
secret, a payload or a database message (a database error is reported by its class only, as ``runworker`` does).
"""

import argparse
import json
from contextlib import contextmanager

from django.core.management.base import CommandError
from django.db import DatabaseError, connections

from apps.modules.errors import RegistryError

from . import rules
from .errors import ModuleHostError

JSON_HELP = "Print one stable JSON document (sorted keys, format 1) instead of text."


def module_id_argument(text: str) -> str:
    """argparse ``type=``: a module id of the documented shape (it is looked up in the registry afterwards; this only refuses garbage early)."""
    try:
        return rules.check_module_id(text)
    except ModuleHostError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def to_json(document: dict) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True)


def short(fingerprint: str) -> str:
    return fingerprint[:12]


@contextmanager
def guarded():
    """Turn the host's refusals into ``CommandError`` (a non-zero exit) and close the database connections afterwards."""
    try:
        yield
    except ModuleHostError as error:
        raise CommandError(f"{error.code}: {error}") from None
    except RegistryError as error:
        raise CommandError(f"the static module registry is invalid ({error.code}); nothing was changed") from None
    except DatabaseError as error:
        raise CommandError(f"Database error ({type(error).__name__}); nothing was changed unless a transaction committed before it.") from None
    finally:
        connections.close_all()
