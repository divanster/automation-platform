"""DB-free tests of the static provider registry: determinism, duplicates, immutability, no discovery, no external calls."""

import itertools
import os
import socket
import unittest
from unittest import mock

from apps.ai_gateway import AIGatewayError, DataLocality, ProviderRegistry
from apps.ai_gateway.adapters.deterministic import DeterministicTestProvider
from tests.ai_gateway_support import ScriptedProvider


class BuildTests(unittest.TestCase):
    def test_an_empty_registry_is_valid(self):
        registry = ProviderRegistry.build(())
        self.assertEqual((len(registry), registry.provider_ids), (0, ()))

    def test_providers_are_ordered_by_id_whatever_the_input_order(self):
        providers = [ScriptedProvider(pid) for pid in ("charlie", "alpha", "bravo")]
        orders = {ProviderRegistry.build(list(p)).provider_ids for p in itertools.permutations(providers)}
        self.assertEqual(orders, {("alpha", "bravo", "charlie")})

    def test_the_registry_is_a_mapping_like_read_only_view(self):
        registry = ProviderRegistry.build([ScriptedProvider("alpha"), ScriptedProvider("bravo")])
        self.assertEqual((len(registry), list(registry)), (2, ["alpha", "bravo"]))
        self.assertIn("alpha", registry)
        self.assertNotIn("zulu", registry)
        self.assertEqual(registry.descriptor("alpha").provider_id, "alpha")
        self.assertEqual(registry.provider("bravo").descriptor.provider_id, "bravo")
        for lookup in (registry.provider, registry.descriptor):
            with self.assertRaises(AIGatewayError) as ctx:
                lookup("zulu")
            self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "unknown_provider"))

    def test_duplicate_provider_ids_are_rejected_not_overwritten(self):
        with self.assertRaises(AIGatewayError) as ctx:
            ProviderRegistry.build([ScriptedProvider("alpha"), ScriptedProvider("bravo"), ScriptedProvider("alpha", DataLocality.CLOUD)])
        self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "duplicate_provider_id"))

    def test_only_provider_objects_are_accepted(self):
        for bad in (
            ["apps.ai_gateway.adapters.deterministic.DeterministicTestProvider"],  # a dotted path
            [DeterministicTestProvider],  # a class, not an instance
            ["alpha"],
            [{"provider_id": "alpha"}],
            [None],
            [object()],
        ):
            with self.subTest(bad), self.assertRaises(AIGatewayError) as ctx:
                ProviderRegistry.build(bad)
            self.assertEqual(ctx.exception.reason, "invalid_provider_object")
        for bad in ("alpha", b"alpha", None, 5):
            with self.subTest(bad), self.assertRaises(AIGatewayError) as ctx:
                ProviderRegistry.build(bad)
            self.assertEqual(ctx.exception.reason, "invalid_provider_collection")

    def test_a_provider_with_a_foreign_descriptor_is_rejected(self):
        class Broken(ScriptedProvider):
            @property
            def descriptor(self):
                return {"provider_id": "x"}

        with self.assertRaises(AIGatewayError) as ctx:
            ProviderRegistry.build([Broken()])
        self.assertEqual(ctx.exception.reason, "invalid_provider_descriptor")

    def test_a_failed_build_builds_nothing(self):
        with self.assertRaises(AIGatewayError):
            ProviderRegistry.build([ScriptedProvider("alpha"), object()])

    def test_the_registry_cannot_be_constructed_directly(self):
        with self.assertRaises(TypeError):
            ProviderRegistry()
        with self.assertRaises(TypeError):
            ProviderRegistry(providers=(), descriptors=())


class ImmutabilityTests(unittest.TestCase):
    def test_no_attribute_can_be_set_or_deleted(self):
        registry = ProviderRegistry.build([ScriptedProvider("alpha")])
        for name in ("_providers", "_descriptors", "anything"):
            with self.subTest(name):
                with self.assertRaises(AttributeError):
                    setattr(registry, name, ())
                with self.assertRaises(AttributeError):
                    delattr(registry, name)

    def test_changing_the_input_list_afterwards_changes_nothing(self):
        providers = [ScriptedProvider("alpha")]
        registry = ProviderRegistry.build(providers)
        providers.append(ScriptedProvider("bravo"))
        providers.clear()
        self.assertEqual(registry.provider_ids, ("alpha",))

    def test_the_descriptor_is_a_snapshot_taken_at_build(self):
        provider = ScriptedProvider("alpha", DataLocality.LOCAL)
        registry = ProviderRegistry.build([provider])
        provider._descriptor = ScriptedProvider("alpha", DataLocality.CLOUD).descriptor  # a provider changing its mind later
        self.assertIs(registry.descriptor("alpha").locality, DataLocality.LOCAL)

    def test_the_provider_tuple_cannot_be_mutated_through_the_api(self):
        registry = ProviderRegistry.build([ScriptedProvider("alpha")])
        self.assertIsInstance(registry.provider_ids, tuple)


class NoDiscoveryAndNoExternalCallTests(unittest.TestCase):
    def test_pythonpath_and_environment_do_not_add_providers(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": os.getcwd(), "AI_GATEWAY_PROVIDERS": "evil.module", "AP_AI_PROVIDER": "x"}):
            self.assertEqual(ProviderRegistry.build(()).provider_ids, ())

    def test_building_a_registry_reads_descriptors_and_calls_nothing_else(self):
        provider = ScriptedProvider("alpha")
        ProviderRegistry.build([provider])
        self.assertEqual(provider.calls, [])

    def test_building_a_registry_makes_no_network_call(self):
        def refuse(*args, **kwargs):
            raise AssertionError("network access attempted")

        with mock.patch("socket.socket", refuse), mock.patch("socket.create_connection", refuse), mock.patch("socket.getaddrinfo", refuse):
            ProviderRegistry.build([DeterministicTestProvider(), ScriptedProvider("alpha")])
        self.assertTrue(hasattr(socket, "socket"))
