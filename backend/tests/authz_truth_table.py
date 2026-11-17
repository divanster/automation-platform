"""The complete input space of the Request Intake access rules, as a deterministic truth table. TEST SUPPORT ONLY.

``rows(permission_problem, scope_problem, access_problem)`` evaluates the three public rule functions on every combination of
action x permission subset x superuser flag x (actor, submitter) scenario and returns one text line per combination, in a fixed
order. ``digest`` is the SHA-256 of those lines. The digest in ``tests/test_authz_equivalence.py`` was computed from the code as it
was BEFORE the refactor (MOD-1b / S1b), so it is the recorded baseline the refactored code must reproduce.
"""

import hashlib
import itertools

ACTIONS = ("submit", "view", "edit", "approve", "reject", "view_audit")

# Every permission the Request Intake rules know, plus two they do not (a similarly named one and a foreign one), so a permission that
# must grant nothing is part of the table.
PERMISSION_UNIVERSE = (
    "intake.submit_request",
    "intake.view_own_request",
    "intake.edit_own_request",
    "intake.view_any_request",
    "intake.approve_request",
    "intake.reject_request",
    "audit.view_auditevent",
    "intake.delete_request",
    "other.view_thing",
)

# (actor pk, submitter pk): the same user; another user; no submitter known; pk 0 (falsy but valid); neither known; an equal-looking
# value of another type.
SCENARIOS = ((1, 1), (1, 2), (1, None), (0, 0), (None, None), (1, "1"))


def subsets(universe=PERMISSION_UNIVERSE):
    for size in range(len(universe) + 1):
        for chosen in itertools.combinations(universe, size):
            yield frozenset(chosen)


def rows(permission_problem, scope_problem, access_problem):
    for action in ACTIONS:
        for held in subsets():
            names = ",".join(sorted(held))
            for superuser in (False, True):
                for actor, submitter in SCENARIOS:
                    first = permission_problem(action, is_superuser=superuser, permissions=held)
                    second = scope_problem(action, is_superuser=superuser, permissions=held, actor_pk=actor, submitter_pk=submitter)
                    both = access_problem(action, is_superuser=superuser, permissions=held, actor_pk=actor, submitter_pk=submitter)
                    yield f"{action}|{names}|{superuser}|{actor!r}|{submitter!r}|{first}|{second}|{both}"


def digest(permission_problem, scope_problem, access_problem) -> str:
    sha = hashlib.sha256()
    count = 0
    for line in rows(permission_problem, scope_problem, access_problem):
        sha.update(line.encode("utf-8") + b"\n")
        count += 1
    return f"{count}:{sha.hexdigest()}"
