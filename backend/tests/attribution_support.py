"""A neutral workflow attribution for tests of Core persistence that are not about Request Intake. Plain data; no database."""

from apps.cases.attribution import WorkflowAttribution

# Not a registered module: Core stamps whatever valid attribution its caller passes, and these tests need one.
DEMO_ATTRIBUTION = WorkflowAttribution("demo_module", "demo_module.review", 1)
