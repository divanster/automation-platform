"""Local-development ``.env`` loading: a small, strict reader. Standard library only; it does not import Django.

WHEN IT RUNS. Only ``manage.py`` calls it (``load_local_env``), and only when the settings module in use is
``config.settings.development``. ``wsgi.py``, ``asgi.py``, the production settings and every settings module never
import this file, so a production process never reads a ``.env`` file. A test pins all of that.

WHAT IT READS. The file ``.env`` in the repository root (git-ignored; ``.env.example`` documents it with placeholders).
A missing file is not an error: the variables then come from the process environment alone, and a missing database
configuration fails when a connection is opened (there is no SQLite fallback and none is added here).

PRECEDENCE. A variable that already exists in the process environment is never changed, even if it is empty: the
environment wins, the file only fills in what is not set.

FORMAT (deliberately small; nothing is expanded or executed):

* one ``NAME=value`` per line, optionally preceded by ``export ``; blank lines and lines starting with ``#`` are ignored;
* only names that match ``AP_[A-Z0-9_]+`` are accepted, so the file cannot set ``PATH``, ``PYTHONPATH``,
  ``DJANGO_SETTINGS_MODULE`` or anything else outside this application's own namespace;
* the value is everything after the first ``=``, with surrounding whitespace removed. There are NO inline comments, so a
  ``#`` inside a value (a password, say) is kept. A value may be wrapped in one pair of single or double quotes, which are
  removed (use them to keep leading or trailing spaces); no escape sequences, no ``$VAR`` expansion;
* a name may appear once; UTF-8, with or without a byte-order mark (Windows Notepad adds one); LF or CRLF line ends.

The whole file is parsed before anything is applied, so a bad line changes nothing.

SECRETS. Error messages name a line number and the rule that was broken, never a value (and never the text of a line that
could not be parsed). This module does not log or print. The result lists variable NAMES only.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEVELOPMENT_SETTINGS = "config.settings.development"

_NAME = re.compile(r"AP_[A-Z0-9_]+")
_LINE_BREAK = re.compile(r"\r\n|\n|\r")


class LocalEnvError(Exception):
    """The ``.env`` file cannot be used. The message never contains a value from the file."""


@dataclass(frozen=True)
class LoadResult:
    path: Path
    found: bool
    loaded: tuple[str, ...]  # names set from the file
    already_set: tuple[str, ...]  # names in the file that the environment already defined (the environment won)


def default_path() -> Path:
    """``.env`` in the repository root (``backend/config/localenv.py`` -> two levels above ``backend/config``)."""
    return Path(__file__).resolve().parents[2] / ".env"


def parse(text: str) -> dict[str, str]:
    """The ``NAME -> value`` pairs of a ``.env`` text, or ``LocalEnvError`` naming the line and the rule."""
    values: dict[str, str] = {}
    for number, raw in enumerate(_LINE_BREAK.split(text), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export") :].lstrip()
        name, separator, value = line.partition("=")
        if not separator:
            raise LocalEnvError(f"line {number}: expected NAME=value")
        name = name.strip()
        if not _NAME.fullmatch(name):
            raise LocalEnvError(f"line {number}: only variable names of the form AP_NAME (capital letters, digits, underscore) are allowed")
        value = value.strip()
        if value[:1] in ('"', "'"):
            quote = value[0]
            if len(value) < 2 or not value.endswith(quote):
                raise LocalEnvError(f"line {number}: a quoted value must end with the same quote and nothing may follow it")
            value = value[1:-1]
        if "\x00" in value:
            raise LocalEnvError(f"line {number}: a value cannot contain a NUL character")
        if name in values:
            raise LocalEnvError(f"line {number}: {name} is defined more than once")
        values[name] = value
    return values


def load_env_file(path: Path | str | None = None, environ=None) -> LoadResult:
    """Read ``path`` (default: the repository's ``.env``) and add what ``environ`` (default ``os.environ``) lacks."""
    path = default_path() if path is None else Path(path)
    environ = os.environ if environ is None else environ
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return LoadResult(path, False, (), ())
    except OSError:
        raise LocalEnvError("the .env file exists but cannot be read") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LocalEnvError("the .env file is not valid UTF-8") from None

    values = parse(text)  # everything is validated before anything is applied
    loaded, already_set = [], []
    for name, value in values.items():
        if name in environ:
            already_set.append(name)
        else:
            environ[name] = value
            loaded.append(name)
    return LoadResult(path, True, tuple(loaded), tuple(already_set))


def requested_settings_module(argv, environ=None) -> str:
    """The settings module ``manage.py`` is about to use: ``--settings`` on the command line, else the environment."""
    environ = os.environ if environ is None else environ
    for index, argument in enumerate(argv):
        if argument == "--settings" and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith("--settings="):
            return argument.split("=", 1)[1]
    return environ.get("DJANGO_SETTINGS_MODULE", "")


def load_local_env(settings_module: str, path: Path | str | None = None, environ=None) -> LoadResult | None:
    """Load ``.env`` for the development settings only. Any other settings module reads nothing and returns None."""
    if settings_module != DEVELOPMENT_SETTINGS:
        return None
    return load_env_file(path, environ)
