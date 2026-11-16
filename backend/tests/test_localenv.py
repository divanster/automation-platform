"""DB-free tests for local ``.env`` loading (``config/localenv.py`` and ``manage.py``).

Every file here is written to a temporary directory with made-up values. No real ``.env`` is read or created, and no
database is touched.
"""

import ast
import contextlib
import io
import logging
import os
import re
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from config import env
from config import localenv
from config.localenv import LocalEnvError, load_env_file, load_local_env, parse, requested_settings_module

from .settings_loader import load_settings

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
SECRET = "S3cr3t-Value!#x"


def _tmp_file(testcase, content: bytes | str, name=".env") -> Path:
    directory = tempfile.TemporaryDirectory()
    testcase.addCleanup(directory.cleanup)
    path = Path(directory.name) / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


class ParseTests(SimpleTestCase):
    def test_reads_names_and_values(self):
        self.assertEqual(parse("AP_DB_NAME=automation_dev\nAP_DB_PORT=5432\n"), {"AP_DB_NAME": "automation_dev", "AP_DB_PORT": "5432"})

    def test_ignores_blank_lines_and_full_line_comments(self):
        self.assertEqual(parse("\n# a comment\n   # indented comment\nAP_DB_USER=u\n\n"), {"AP_DB_USER": "u"})

    def test_accepts_an_export_prefix(self):
        self.assertEqual(parse("export AP_DB_USER=u"), {"AP_DB_USER": "u"})

    def test_surrounding_whitespace_is_removed(self):
        self.assertEqual(parse("  AP_DB_USER  =   u v   "), {"AP_DB_USER": "u v"})

    def test_a_value_may_contain_equals_and_hash_signs_because_there_are_no_inline_comments(self):
        self.assertEqual(parse("AP_DB_PASSWORD=ab=cd#ef gh"), {"AP_DB_PASSWORD": "ab=cd#ef gh"})

    def test_one_pair_of_quotes_is_removed_and_keeps_inner_spaces(self):
        text = "AP_SECRET_KEY=\"  padded  \"\nAP_DB_USER='single quoted'\nAP_DB_NAME=\"it's\""
        self.assertEqual(parse(text), {"AP_SECRET_KEY": "  padded  ", "AP_DB_USER": "single quoted", "AP_DB_NAME": "it's"})

    def test_nothing_is_expanded_or_interpreted(self):
        self.assertEqual(parse(r"AP_DB_PASSWORD=$HOME\n%PATH%`id`"), {"AP_DB_PASSWORD": r"$HOME\n%PATH%`id`"})

    def test_an_empty_value_is_allowed(self):
        self.assertEqual(parse("AP_DB_PASSWORD="), {"AP_DB_PASSWORD": ""})

    def test_line_endings(self):
        self.assertEqual(parse("AP_DB_USER=a\r\nAP_DB_NAME=b\rAP_DB_HOST=c\n"), {"AP_DB_USER": "a", "AP_DB_NAME": "b", "AP_DB_HOST": "c"})

    def test_every_variable_of_the_example_file_parses(self):
        example = (REPO / ".env.example").read_text(encoding="utf-8")
        parsed = parse(example)
        self.assertTrue({"AP_DB_NAME", "AP_DB_USER", "AP_DB_PASSWORD", "AP_DB_HOST", "AP_DB_PORT"} <= set(parsed))
        # The example holds placeholders only: no password, no secret key.
        self.assertEqual(parsed["AP_DB_PASSWORD"], "")
        self.assertEqual(parsed.get("AP_SECRET_KEY", ""), "")

    def test_rejects_a_line_without_equals(self):
        with self.assertRaisesRegex(LocalEnvError, r"^line 2: expected NAME=value$"):
            parse("AP_DB_USER=u\nnot a setting")

    def test_only_ap_variable_names_are_accepted(self):
        for line in ("PATH=/x", "PYTHONPATH=/x", "DJANGO_SETTINGS_MODULE=config.settings.production", "ap_db_name=x", "AP_db=x", "AP_=x", "AP DB=x", "=x"):
            with self.subTest(line=line), self.assertRaisesRegex(LocalEnvError, r"line 1: only variable names"):
                parse(line)

    def test_rejects_broken_quotes(self):
        for line in ('AP_DB_USER="abc', 'AP_DB_USER="abc" # c', 'AP_DB_USER="abc"def', "AP_DB_USER='", 'AP_DB_USER="'):
            with self.subTest(line=line), self.assertRaisesRegex(LocalEnvError, r"quoted value"):
                parse(line)

    def test_rejects_a_name_defined_twice(self):
        with self.assertRaisesRegex(LocalEnvError, r"line 2: AP_DB_USER is defined more than once"):
            parse("AP_DB_USER=a\nAP_DB_USER=b")

    def test_rejects_a_nul_character(self):
        with self.assertRaisesRegex(LocalEnvError, r"NUL"):
            parse("AP_DB_USER=a\x00b")


class LoadFileTests(SimpleTestCase):
    def test_loads_missing_variables_and_reports_names_only(self):
        path = _tmp_file(self, "AP_DB_NAME=n\nAP_DB_USER=u\n")
        environ = {}
        result = load_env_file(path, environ)
        self.assertEqual(environ, {"AP_DB_NAME": "n", "AP_DB_USER": "u"})
        self.assertTrue(result.found)
        self.assertEqual(result.loaded, ("AP_DB_NAME", "AP_DB_USER"))
        self.assertEqual(result.already_set, ())

    def test_existing_environment_variables_take_precedence(self):
        path = _tmp_file(self, "AP_DB_NAME=from_file\nAP_DB_USER=from_file\n")
        environ = {"AP_DB_NAME": "from_environment"}
        result = load_env_file(path, environ)
        self.assertEqual(environ, {"AP_DB_NAME": "from_environment", "AP_DB_USER": "from_file"})
        self.assertEqual(result.loaded, ("AP_DB_USER",))
        self.assertEqual(result.already_set, ("AP_DB_NAME",))

    def test_an_existing_but_empty_variable_still_wins(self):
        path = _tmp_file(self, "AP_DB_PASSWORD=from_file\n")
        environ = {"AP_DB_PASSWORD": ""}
        load_env_file(path, environ)
        self.assertEqual(environ["AP_DB_PASSWORD"], "")

    def test_a_missing_file_is_not_an_error_and_changes_nothing(self):
        path = Path(tempfile.gettempdir()) / "does-not-exist-for-ap-tests" / ".env"
        environ = {"AP_DB_NAME": "kept"}
        result = load_env_file(path, environ)
        self.assertFalse(result.found)
        self.assertEqual((result.loaded, result.already_set), ((), ()))
        self.assertEqual(environ, {"AP_DB_NAME": "kept"})

    def test_a_bad_line_changes_nothing_even_after_good_lines(self):
        path = _tmp_file(self, "AP_DB_NAME=n\nthis is broken\n")
        environ = {}
        with self.assertRaises(LocalEnvError):
            load_env_file(path, environ)
        self.assertEqual(environ, {})

    def test_reads_a_windows_file_with_bom_and_crlf(self):
        path = _tmp_file(self, b"\xef\xbb\xbfAP_DB_NAME=n\r\nAP_DB_USER=u\r\n")
        environ = {}
        load_env_file(path, environ)
        self.assertEqual(environ, {"AP_DB_NAME": "n", "AP_DB_USER": "u"})

    def test_a_file_that_is_not_utf8_is_refused_without_showing_bytes(self):
        path = _tmp_file(self, b"AP_DB_PASSWORD=\xff\xfe\xfd\n")
        with self.assertRaisesRegex(LocalEnvError, r"^the \.env file is not valid UTF-8$"):
            load_env_file(path, {})

    def test_a_path_that_cannot_be_read_is_reported(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with self.assertRaisesRegex(LocalEnvError, r"cannot be read"):
            load_env_file(Path(directory.name), {})  # a directory, not a file

    def test_the_default_path_is_the_repository_root(self):
        self.assertEqual(localenv.default_path(), REPO / ".env")
        self.assertTrue((REPO / "backend" / "manage.py").exists())


class ProcessEnvironmentTests(SimpleTestCase):
    """The loaded values are what the settings helpers (``config.env``) actually read."""

    def _clean_environment(self):
        cleaned = {k: v for k, v in os.environ.items() if not k.startswith("AP_")}
        patcher = mock.patch.dict(os.environ, cleaned, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_settings_helpers_see_the_loaded_values(self):
        self._clean_environment()
        path = _tmp_file(self, "AP_DB_NAME=from_file\nAP_DB_CONNECT_TIMEOUT=7\n")
        load_env_file(path)
        self.assertEqual(env.get_str("AP_DB_NAME"), "from_file")
        self.assertEqual(env.get_int("AP_DB_CONNECT_TIMEOUT", 3, minimum=1), 7)

    def test_a_variable_set_in_the_shell_beats_the_file(self):
        self._clean_environment()
        os.environ["AP_DB_NAME"] = "from_shell"
        load_env_file(_tmp_file(self, "AP_DB_NAME=from_file\n"))
        self.assertEqual(env.get_str("AP_DB_NAME"), "from_shell")

    def test_the_database_is_still_postgresql_and_unconfigured_without_any_source(self):
        # No file and no variables: the settings keep PostgreSQL with an empty name, so opening a connection fails.
        # Nothing falls back to another database.
        settings = load_settings("config.settings.development")
        database = settings.DATABASES["default"]
        self.assertEqual(database["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(database["NAME"], "")
        self.assertEqual(set(settings.DATABASES), {"default"})

    def test_no_settings_module_references_a_sqlite_engine_or_driver(self):
        # Comments may say "there is no SQLite fallback"; what must not exist is a SQLite engine or driver.
        for relative in ("config/settings/base.py", "config/settings/development.py", "config/settings/production.py", "config/localenv.py", "manage.py"):
            with self.subTest(file=relative):
                self.assertNotRegex((BACKEND / relative).read_text(encoding="utf-8").lower(), r"backends\.sqlite|sqlite3")


class DevelopmentOnlyTests(SimpleTestCase):
    def test_the_development_settings_load_the_file(self):
        path = _tmp_file(self, "AP_DB_NAME=n\n")
        environ = {}
        result = load_local_env("config.settings.development", path, environ)
        self.assertIsNotNone(result)
        self.assertEqual(environ, {"AP_DB_NAME": "n"})

    def test_production_and_every_other_settings_module_read_nothing(self):
        path = _tmp_file(self, "AP_SECRET_KEY=from-file\nAP_ALLOWED_HOSTS=example.invalid\n")
        for module in ("config.settings.production", "config.settings.base", "", "something.else"):
            with self.subTest(module=module):
                environ = {}
                self.assertIsNone(load_local_env(module, path, environ))
                self.assertEqual(environ, {})

    def test_a_production_run_does_not_read_a_file_that_is_not_even_parseable(self):
        path = _tmp_file(self, "this would be an error")
        self.assertIsNone(load_local_env("config.settings.production", path, {}))

    def test_the_settings_flag_decides_before_the_environment_variable(self):
        environ = {"DJANGO_SETTINGS_MODULE": "config.settings.development"}
        self.assertEqual(requested_settings_module(["manage.py", "check"], environ), "config.settings.development")
        self.assertEqual(requested_settings_module(["manage.py", "check", "--settings=config.settings.production"], environ), "config.settings.production")
        self.assertEqual(requested_settings_module(["manage.py", "check", "--settings", "config.settings.production"], environ), "config.settings.production")
        self.assertEqual(requested_settings_module(["manage.py"], {}), "")

    def test_the_flag_makes_a_production_command_skip_the_file(self):
        path = _tmp_file(self, "AP_SECRET_KEY=from-file\n")
        environ = {"DJANGO_SETTINGS_MODULE": "config.settings.development"}
        module = requested_settings_module(["manage.py", "check", "--settings=config.settings.production"], environ)
        self.assertIsNone(load_local_env(module, path, environ))
        self.assertNotIn("AP_SECRET_KEY", environ)


def _imports_of(relative: str) -> set[str]:
    tree = ast.parse((BACKEND / relative).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


class WiringTests(SimpleTestCase):
    def test_production_entry_points_and_settings_never_import_the_loader(self):
        for relative in ("config/wsgi.py", "config/asgi.py", "config/env.py", "config/settings/base.py", "config/settings/development.py", "config/settings/production.py"):
            with self.subTest(file=relative):
                self.assertFalse({name for name in _imports_of(relative) if "localenv" in name}, relative)

    def test_manage_py_loads_the_file_before_django_starts(self):
        source = (BACKEND / "manage.py").read_text(encoding="utf-8")
        self.assertIn("config.localenv", _imports_of("manage.py"))
        self.assertLess(source.index("load_local_env("), source.index("execute_from_command_line(sys.argv)"))
        self.assertIn("requested_settings_module(sys.argv", source)

    def test_no_dotenv_package_is_a_dependency(self):
        for name in ("runtime.in", "development.in", "runtime.txt", "development.txt"):
            with self.subTest(file=name):
                self.assertNotRegex((REPO / "requirements" / name).read_text(encoding="utf-8").lower(), r"dotenv")

    def test_the_real_env_file_is_git_ignored_and_the_example_is_not(self):
        lines = [line.strip() for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()]
        self.assertIn(".env", lines)
        self.assertIn(".env.*", lines)
        self.assertIn("!.env.example", lines)
        self.assertLess(lines.index(".env.*"), lines.index("!.env.example"))

    def test_the_loader_imports_only_the_standard_library(self):
        allowed = {"os", "re", "dataclasses", "pathlib", "dataclasses.dataclass", "pathlib.Path"}
        self.assertTrue(_imports_of("config/localenv.py") <= allowed, _imports_of("config/localenv.py") - allowed)


class NoSecretsInOutputTests(SimpleTestCase):
    def _capture(self, action):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertNoLogs(logging.getLogger(), level="DEBUG"):
            try:
                result = action()
            except LocalEnvError as error:
                return out.getvalue() + err.getvalue(), str(error), repr(error)
        return out.getvalue() + err.getvalue(), repr(result), str(result)

    def test_a_successful_load_prints_logs_and_reports_no_value(self):
        path = _tmp_file(self, f'AP_DB_PASSWORD="{SECRET}"\nAP_SECRET_KEY={SECRET}\n')
        for text in self._capture(lambda: load_env_file(path, {})):
            self.assertNotIn(SECRET, text)
            self.assertNotIn(SECRET.strip("!#x"), text)

    def test_every_kind_of_error_leaves_the_values_out_of_the_message(self):
        bad_files = [
            f"AP_DB_PASSWORD={SECRET}\nthe password is {SECRET}",  # no equals
            f"password={SECRET}",  # wrong name
            f'AP_DB_PASSWORD="{SECRET}',  # unterminated quote
            f'AP_DB_PASSWORD="{SECRET}" trailing',  # text after the quote
            f"AP_DB_PASSWORD={SECRET}\nAP_DB_PASSWORD={SECRET}",  # duplicate
            f"AP_DB_PASSWORD={SECRET}\x00",  # NUL
        ]
        for content in bad_files:
            with self.subTest(content=content[:24]):
                path = _tmp_file(self, content)
                for text in self._capture(lambda: load_env_file(path, {})):
                    self.assertNotIn(SECRET, text)
                    self.assertNotIn("S3cr3t", text)

    def test_the_result_object_holds_names_not_values(self):
        path = _tmp_file(self, f"AP_DB_PASSWORD={SECRET}\n")
        result = load_env_file(path, {})
        self.assertEqual(result.loaded, ("AP_DB_PASSWORD",))
        self.assertNotIn(SECRET, repr(result))

    def test_manage_py_reports_a_bad_file_with_the_rule_only(self):
        source = (BACKEND / "manage.py").read_text(encoding="utf-8")
        self.assertRegex(source, re.compile(r'sys\.exit\(f"Error in \.env[^"]*\{exc\}"\)'))
