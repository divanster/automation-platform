"""The composition root of ModuleContext: the one place that hands a module its context (MOD-1b S6). It names no module.

``module_context(module_id)`` opens the context of a module in the approved static registry (``config.modules``: Request Intake since MOD-2), with the
production task handler allowlist (``apps.intake.registry.PRODUCTION_REGISTRY``, the same one the worker uses). Nothing in the application calls it yet: Request
Intake is a registered module (MOD-2), so ``module_context("request_intake")`` opens, but its submission path does not use a context. Core
packages never import this file (a test pins that). Importing it reads no database and no file.
"""

from apps.intake.registry import PRODUCTION_REGISTRY
from apps.module_host.context import ModuleContext
from config.modules import build_registry

REGISTRY = build_registry()


def module_context(module_id: str) -> ModuleContext:
    """The context of ``module_id``. Raises ``ModuleContextError`` (``unknown_module``, ``invalid_module_id``)."""
    return ModuleContext.open(REGISTRY, module_id, handler_registry=PRODUCTION_REGISTRY)
