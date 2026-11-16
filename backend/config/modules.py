"""The composition root for built-in modules: the ONE place that may name them. Standard library plus ``apps.modules`` only.

``BUILTIN_MODULES`` is a static tuple of manifest objects that are approved to be part of this Core release. A module is built in
only if its manifest is written here, in code, in a reviewed change: there is no scanning of directories, no setting that lists
dotted paths, no entry points and no import by name. Core packages (``apps.accounts``, ``apps.audit``, ``apps.cases``,
``apps.tasks``, ``apps.modules``) never import this file or any module; a test pins that.

MOD-1a: the tuple is EMPTY. No module is registered, nothing is activated, and nothing in the application calls
``build_registry`` yet. Registering Request Intake as the first built-in module is MOD-2.

WHEN A MODULE IS ADDED. Import its manifest here, inside the module's own package (for example ``from apps.intake.module import
MANIFEST``), not in Core. Do it lazily if the import needs Django models: this file must stay importable before Django's app
registry is ready, so it must not import module packages at module level once they depend on models; MOD-2 decides the exact
mechanism (docs/architecture/module-contract.md, "Open decisions").
"""

from apps.modules.manifest import ModuleManifest
from apps.modules.registry import ModuleRegistry

BUILTIN_MODULES: tuple[ModuleManifest, ...] = ()


def build_registry(manifests: tuple[ModuleManifest, ...] | None = None) -> ModuleRegistry:
    """Build the registry from the approved built-in manifests (or from ``manifests``, for tests). Raises ``RegistryError`` if they do not fit."""
    return ModuleRegistry.build(BUILTIN_MODULES if manifests is None else manifests)
