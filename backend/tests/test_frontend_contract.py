"""DB-free guard: the few constants the frontend mirrors from the backend must not drift.

The REST API has no endpoint that lists the rejection reasons or the run states, so ``frontend/src/api/types.ts``
repeats them. This test reads that one file as text and compares the lists with the backend's own, so a reason or a state
added on one side without the other fails here instead of surfacing as a confusing 400 in the browser. It checks nothing
else about the frontend and imports no frontend tooling.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

from apps.cases.run_state import STATES
from apps.intake.access import Action
from apps.intake.policy import REJECTION_REASONS

TYPES_TS = Path(__file__).resolve().parent.parent.parent / "frontend" / "src" / "api" / "types.ts"


def _tuple_constant(source: str, name: str) -> list[str]:
    match = re.search(rf"export const {name} = \[(.*?)\] as const;", source, re.DOTALL)
    assert match, f"{name} not found in {TYPES_TS}"
    return re.findall(r'"([^"]+)"', match.group(1))


class FrontendContractTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = TYPES_TS.read_text(encoding="utf-8")

    def test_rejection_reasons_match_the_backend_in_content_and_order(self):
        self.assertEqual(_tuple_constant(self.source, "REJECTION_REASONS"), list(REJECTION_REASONS))

    def test_run_states_match_the_backend(self):
        self.assertEqual(_tuple_constant(self.source, "RUN_STATES"), list(STATES))

    def test_review_actions_are_actions_the_backend_knows(self):
        match = re.search(r"export type ReviewAction = ([^;]+);", self.source)
        self.assertIsNotNone(match)
        frontend = set(re.findall(r'"([^"]+)"', match.group(1)))
        self.assertEqual(frontend, {"edit", "approve", "reject"})
        self.assertLessEqual(frontend, {action.value for action in Action})
