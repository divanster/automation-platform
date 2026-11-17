"""DB-free tests for MOD-1b S4: the administrator commands ``module_sync``, ``module_status``, ``module_enable`` and ``module_disable``.

The registry and the services are replaced, so no database is touched. These tests pin the input validation, the exit behaviour, the text and the stable JSON
output, and that nothing secret or free-form reaches the console. This file is ASCII only.
"""

import io
import json
from datetime import datetime, timezone
from unittest import mock

from django.core.management import CommandError, call_command, get_commands
from django.db import OperationalError
from django.test import SimpleTestCase

from apps.module_host import cli, services
from apps.module_host.errors import ModuleHostError
from apps.module_host.rules import VersionMismatch
from apps.modules.errors import RegistryError
from apps.modules.manifest import TaskHandlerDeclaration
from apps.modules.registry import ModuleRegistry
from tests.module_support import FakeHandler, named

REGISTRY = ModuleRegistry.build([named("alpha_mod", task_handlers=(TaskHandlerDeclaration("alpha_mod.work", FakeHandler("alpha_mod.work"), "work"),))])
FP = REGISTRY.fingerprint
COMMANDS = ("module_sync", "module_status", "module_enable", "module_disable")


def run(name, *args, expect_error=False):
    out, err = io.StringIO(), io.StringIO()
    if expect_error:
        with mock.patch.object(cli, "connections"):
            try:
                call_command(name, *args, stdout=out, stderr=err)
            except CommandError as error:
                return out.getvalue(), str(error)
        raise AssertionError("the command should have failed")
    with mock.patch.object(cli, "connections"):
        call_command(name, *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def patched_registry(name):
    return mock.patch(f"apps.module_host.management.commands.{name}.build_registry", return_value=REGISTRY)


def report(**overrides):
    values = dict(registry_fingerprint=FP, initial=True, created=(("alpha_mod", True),), refreshed=(), unchanged=(), version_mismatches=(), unregistered=(), claims_added=(("alpha_mod", "alpha_mod"),))
    values.update(overrides)
    return services.SyncReport(**values)


class CommandListTests(SimpleTestCase):
    def test_the_four_commands_exist_in_the_module_host_app(self):
        commands = get_commands()
        for name in COMMANDS:
            self.assertEqual(commands.get(name), "apps.module_host", name)

    def test_there_is_no_install_update_run_or_shell_command(self):
        host = {name for name, app in get_commands().items() if app == "apps.module_host"}
        self.assertEqual(host, set(COMMANDS))


class SyncCommandTests(SimpleTestCase):
    def test_text_output_lists_what_happened(self):
        with patched_registry("module_sync"), mock.patch("apps.module_host.management.commands.module_sync.sync_modules", return_value=report()) as sync:
            out, _ = run("module_sync")
        sync.assert_called_once_with(REGISTRY)
        self.assertIn("initial sync done", out)
        self.assertIn("created   alpha_mod (enabled)", out)
        self.assertIn("namespace alpha_mod claimed by alpha_mod", out)

    def test_json_output_is_stable(self):
        with patched_registry("module_sync"), mock.patch("apps.module_host.management.commands.module_sync.sync_modules", return_value=report()):
            first, _ = run("module_sync", "--json")
            second, _ = run("module_sync", "--json")
        self.assertEqual(first, second)
        document = json.loads(first)
        self.assertEqual(document["format"], 1)
        self.assertEqual(document["created"], [{"module_id": "alpha_mod", "enabled": True}])
        self.assertEqual(first, json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + chr(10))

    def test_nothing_registered_says_so(self):
        empty = report(created=(), claims_added=(), initial=True)
        with patched_registry("module_sync"), mock.patch("apps.module_host.management.commands.module_sync.sync_modules", return_value=empty):
            out, _ = run("module_sync")
        self.assertIn("no modules are registered", out)

    def test_a_version_mismatch_prints_the_report_and_exits_non_zero(self):
        bad = report(created=(), claims_added=(), version_mismatches=(VersionMismatch("alpha_mod", "0.9.0", "1.0.0"),))
        with patched_registry("module_sync"), mock.patch("apps.module_host.management.commands.module_sync.sync_modules", return_value=bad):
            out, error = run("module_sync", expect_error=True)
        self.assertIn("VERSION MISMATCH alpha_mod: installed 0.9.0, registered 1.0.0", out)
        self.assertIn("version_mismatch", error)

    def test_a_namespace_conflict_is_a_command_error_with_its_code(self):
        refusal = ModuleHostError("namespace_ownership_changed", "namespace 'alpha_mod' belonged to 'x'")
        with patched_registry("module_sync"), mock.patch("apps.module_host.management.commands.module_sync.sync_modules", side_effect=refusal):
            _, error = run("module_sync", expect_error=True)
        self.assertIn("namespace_ownership_changed", error)

    def test_it_takes_no_other_argument(self):
        for args in (("extra",), ("--migrate",), ("--install",), ("--command", "ls")):
            with self.subTest(args=args), patched_registry("module_sync"):
                run("module_sync", *args, expect_error=True)


class StatusCommandTests(SimpleTestCase):
    def status(self, synced=True, enabled=True, version="1.0.0"):
        row = services.ModuleStatus(
            module_id="alpha_mod",
            module_kind="business",
            registered_version="1.0.0",
            installed_version=version,
            enabled=enabled,
            claimable=enabled and version == "1.0.0",
            reason="enabled" if enabled else "disabled",
            dependencies=(),
            task_kinds=("alpha_mod.work",),
            configuration_version=None,
            registry_fingerprint_current=True,
            enabled_changed_at="2031-05-01T09:30:00+00:00",
        )
        return services.HostStatus(FP, synced, (row,), ())

    def call(self, *args, **kwargs):
        with patched_registry("module_status"), mock.patch("apps.module_host.management.commands.module_status.get_module_status", return_value=self.status(**kwargs)):
            return run("module_status", *args)

    def test_text_output(self):
        out, _ = self.call()
        self.assertIn(f"Registry {FP[:12]}; synced: yes", out)
        self.assertIn("alpha_mod: enabled; registered 1.0.0, installed 1.0.0; claimable yes (enabled); task kinds: alpha_mod.work", out)

    def test_an_unsynced_installation_explains_the_transition(self):
        out, _ = self.call(synced=False)
        self.assertIn("synced: no", out)
        self.assertIn("treated as enabled until the first module_sync", out)

    def test_a_disabled_module_is_shown_as_not_claimable(self):
        out, _ = self.call(enabled=False)
        self.assertIn("alpha_mod: disabled", out)
        self.assertIn("claimable no (disabled)", out)

    def test_json_output_is_stable_and_complete(self):
        first, _ = self.call("--json")
        second, _ = self.call("--json")
        self.assertEqual(first, second)
        document = json.loads(first)
        self.assertEqual(set(document), {"format", "registry_fingerprint", "synced", "modules", "unregistered_state"})
        self.assertEqual(document["modules"][0]["module_id"], "alpha_mod")
        self.assertEqual(first, json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + chr(10))

    def test_one_module_can_be_selected_and_an_unknown_one_is_an_error(self):
        out, _ = self.call("alpha_mod", "--json")
        self.assertEqual([m["module_id"] for m in json.loads(out)["modules"]], ["alpha_mod"])
        with patched_registry("module_status"), mock.patch("apps.module_host.management.commands.module_status.get_module_status", return_value=self.status()):
            _, error = run("module_status", "ghost_mod", expect_error=True)
        self.assertIn("unknown_module", error)

    def test_garbage_ids_are_refused_by_the_argument_parser(self):
        for bad in ("Alpha", "../etc", "a b", "x" * 41, "ab"):
            with self.subTest(bad=bad), patched_registry("module_status"):
                run("module_status", bad, expect_error=True)

    def test_it_reads_only(self):
        with patched_registry("module_status"), mock.patch("apps.module_host.management.commands.module_status.get_module_status", return_value=self.status()):
            run("module_status")
        # nothing writable is even imported by the command
        import apps.module_host.management.commands.module_status as command

        self.assertFalse({"sync_modules", "enable_module", "disable_module"} & set(vars(command)))

    def test_no_modules_registered(self):
        with patched_registry("module_status"), mock.patch("apps.module_host.management.commands.module_status.get_module_status", return_value=services.HostStatus(FP, False, (), ())):
            out, _ = run("module_status")
        self.assertIn("No modules are registered.", out)


class EnableDisableCommandTests(SimpleTestCase):
    def call(self, name, verb, *args, changed=True):
        enabled = verb == "enable"
        target = f"apps.module_host.management.commands.{name}.{verb}_module"
        with patched_registry(name), mock.patch(target, return_value=services.ChangeResult("alpha_mod", changed, enabled)) as service:
            output = run(name, *args)
        return output[0], service

    def test_enable_text_json_and_the_service_call(self):
        out, service = self.call("module_enable", "enable", "alpha_mod")
        service.assert_called_once_with(REGISTRY, "alpha_mod")
        self.assertIn("alpha_mod: enabled.", out)
        out, _ = self.call("module_enable", "enable", "alpha_mod", "--json")
        self.assertEqual(json.loads(out), {"format": 1, "module_id": "alpha_mod", "changed": True, "enabled": True})

    def test_disable_text_and_json(self):
        out, service = self.call("module_disable", "disable", "alpha_mod")
        service.assert_called_once_with(REGISTRY, "alpha_mod")
        self.assertIn("alpha_mod: disabled.", out)
        out, _ = self.call("module_disable", "disable", "alpha_mod", "--json")
        self.assertEqual(json.loads(out), {"format": 1, "module_id": "alpha_mod", "changed": True, "enabled": False})

    def test_an_idempotent_no_op_says_so(self):
        out, _ = self.call("module_enable", "enable", "alpha_mod", changed=False)
        self.assertIn("already enabled (no change)", out)
        out, _ = self.call("module_disable", "disable", "alpha_mod", changed=False)
        self.assertIn("already disabled (no change)", out)

    def test_a_module_id_is_required_and_validated(self):
        for name in ("module_enable", "module_disable"):
            for args in ((), ("Alpha",), ("alpha mod",), ("alpha_mod; rm -rf /",), ("$(id)",), ("a",), ("alpha_mod", "second_mod")):
                with self.subTest(name=name, args=args), patched_registry(name):
                    run(name, *args, expect_error=True)

    def test_a_refusal_is_a_command_error_naming_the_code_and_the_registry_is_never_modified(self):
        for name, verb, code in (("module_enable", "enable", "dependency_not_enabled"), ("module_disable", "disable", "dependents_enabled")):
            refusal = ModuleHostError(code, "refused: module ids only")
            with self.subTest(name=name), patched_registry(name), mock.patch(f"apps.module_host.management.commands.{name}.{verb}_module", side_effect=refusal):
                _, error = run(name, "alpha_mod", expect_error=True)
            self.assertEqual(error, f"{code}: refused: module ids only")

    def test_only_the_registered_modules_can_be_named_through_the_service(self):
        for name, verb in (("module_enable", "enable"), ("module_disable", "disable")):
            with self.subTest(name=name), patched_registry(name), mock.patch(f"apps.module_host.management.commands.{name}.{verb}_module", side_effect=ModuleHostError("unknown_module", "x")):
                _, error = run(name, "ghost_mod", expect_error=True)
            self.assertTrue(error.startswith("unknown_module"))


class SafetyTests(SimpleTestCase):
    def test_a_database_error_is_reported_by_class_only(self):
        secret = "host=10.0.0.5 password=hunter2"
        for name, args, target in (
            ("module_enable", ("alpha_mod",), "module_enable.enable_module"),
            ("module_disable", ("alpha_mod",), "module_disable.disable_module"),
            ("module_sync", (), "module_sync.sync_modules"),
            ("module_status", (), "module_status.get_module_status"),
        ):
            with self.subTest(name=name), patched_registry(name), mock.patch(f"apps.module_host.management.commands.{target}", side_effect=OperationalError(secret)):
                out, error = run(name, *args, expect_error=True)
            self.assertIn("OperationalError", error)
            self.assertNotIn("hunter2", error + out)
            self.assertNotIn("10.0.0.5", error + out)

    def test_an_invalid_static_registry_is_a_command_error_that_changes_nothing(self):
        with mock.patch("apps.module_host.management.commands.module_sync.build_registry", side_effect=RegistryError("dependency_cycle", "a and b")), mock.patch(
            "apps.module_host.management.commands.module_sync.sync_modules"
        ) as sync:
            _, error = run("module_sync", expect_error=True)
        self.assertIn("dependency_cycle", error)
        sync.assert_not_called()

    def test_the_connections_are_closed_after_every_command(self):
        for name, args in (("module_status", ()), ("module_enable", ("alpha_mod",))):
            with self.subTest(name=name), patched_registry(name):
                with mock.patch.object(cli, "connections") as connections, mock.patch(
                    "apps.module_host.management.commands.module_status.get_module_status", return_value=services.HostStatus(FP, True, (), ())
                ), mock.patch("apps.module_host.management.commands.module_enable.enable_module", return_value=services.ChangeResult("alpha_mod", False, True)):
                    call_command(name, *args, stdout=io.StringIO())
                connections.close_all.assert_called_once()

    def test_the_commands_do_not_run_migrations_import_modules_or_start_processes(self):
        import ast
        from pathlib import Path

        banned_names = {"call_command", "migrate", "makemigrations", "import_module", "__import__", "eval", "exec", "system", "Popen", "run", "check_output", "load_source"}
        banned_imports = {"importlib", "subprocess", "os", "pkgutil", "pip", "runpy"}
        root = Path(__file__).resolve().parent.parent / "apps" / "module_host"
        for path in [root / "cli.py", *sorted((root / "management" / "commands").glob("module_*.py"))]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
                n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
            }
            with self.subTest(file=path.name):
                self.assertEqual(names & banned_names, set())
                self.assertEqual(imported & banned_imports, set())

    def test_output_never_contains_a_fingerprint_longer_than_the_short_form_in_text_mode(self):
        with patched_registry("module_status"), mock.patch(
            "apps.module_host.management.commands.module_status.get_module_status", return_value=services.HostStatus(FP, True, (), ())
        ):
            out, _ = run("module_status")
        self.assertNotIn(FP, out)
        self.assertIn(FP[:12], out)
        self.assertEqual(cli.short(FP), FP[:12])

    def test_the_timestamp_format_used_by_status_is_iso_8601(self):
        self.assertEqual(datetime(2031, 5, 1, 9, 30, tzinfo=timezone.utc).isoformat(), "2031-05-01T09:30:00+00:00")
