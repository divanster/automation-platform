"""DB-free tests for the environment helpers."""

import os
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config import env


def _environ(**values):
    return mock.patch.dict(os.environ, values, clear=False)


class GetStrTests(SimpleTestCase):
    def test_returns_stripped_value(self):
        with _environ(AP_TEST_VALUE="  abc  "):
            self.assertEqual(env.get_str("AP_TEST_VALUE"), "abc")

    def test_empty_counts_as_unset(self):
        with _environ(AP_TEST_VALUE="   "):
            self.assertEqual(env.get_str("AP_TEST_VALUE", "fallback"), "fallback")

    def test_required_raises_when_unset(self):
        os.environ.pop("AP_TEST_MISSING", None)
        with self.assertRaises(ImproperlyConfigured):
            env.get_str("AP_TEST_MISSING", required=True)


class GetBoolTests(SimpleTestCase):
    def test_true_and_false_values(self):
        for text, expected in (("true", True), ("1", True), ("No", False), ("off", False)):
            with self.subTest(text=text), _environ(AP_TEST_FLAG=text):
                self.assertIs(env.get_bool("AP_TEST_FLAG", None), expected)

    def test_default_when_unset(self):
        os.environ.pop("AP_TEST_FLAG", None)
        self.assertIs(env.get_bool("AP_TEST_FLAG", True), True)

    def test_invalid_value_raises(self):
        with _environ(AP_TEST_FLAG="maybe"), self.assertRaises(ImproperlyConfigured):
            env.get_bool("AP_TEST_FLAG", False)


class GetIntTests(SimpleTestCase):
    def test_parses_integer(self):
        with _environ(AP_TEST_NUMBER="7"):
            self.assertEqual(env.get_int("AP_TEST_NUMBER", 1), 7)

    def test_invalid_raises(self):
        with _environ(AP_TEST_NUMBER="seven"), self.assertRaises(ImproperlyConfigured):
            env.get_int("AP_TEST_NUMBER", 1)

    def test_minimum_enforced(self):
        with _environ(AP_TEST_NUMBER="0"), self.assertRaises(ImproperlyConfigured):
            env.get_int("AP_TEST_NUMBER", 1, minimum=1)


class GetListTests(SimpleTestCase):
    def test_splits_and_strips(self):
        with _environ(AP_TEST_LIST=" a, b ,,c "):
            self.assertEqual(env.get_list("AP_TEST_LIST"), ["a", "b", "c"])

    def test_default_when_unset(self):
        os.environ.pop("AP_TEST_LIST", None)
        self.assertEqual(env.get_list("AP_TEST_LIST", default=["x"]), ["x"])
