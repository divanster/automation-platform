"""The composition root for built-in modules: the ONE place that may name them. Standard library plus ``apps.modules`` only.

``BUILTIN_MODULES`` is a static tuple of manifest objects that are approved to be part of this Core release. A module is built in
only if its manifest is written here, in code, in a reviewed change: there is no scanning of directories, no setting that lists
dotted paths, no entry points and no import by name. Core packages (``apps.accounts``, ``apps.audit``, ``apps.cases``,
``apps.tasks``, ``apps.modules``) never import this file or any module; a test pins that.

MOD-2: the tuple holds ONE module, Request Intake (``apps.intake.module.MANIFEST``), the first built-in module. Registering it activates nothing by itself:
the worker gates its task kind on the stored module state (``module_sync`` creates it), and no API route, screen or permission changes.

WHEN A MODULE IS ADDED. Import its manifest here, from the module's own package, by a fixed name in a reviewed change (never from a setting or a scan). The
manifest module must stay importable before Django's app registry is ready: it declares data only and imports no models, no handler and nothing that does.
A test pins that ``apps.intake.module`` imports no Django and no model.
"""

from apps.intake.module import MANIFEST as REQUEST_INTAKE_MANIFEST
from apps.modules.manifest import ModuleManifest
from apps.modules.registry import ModuleRegistry

BUILTIN_MODULES: tuple[ModuleManifest, ...] = (REQUEST_INTAKE_MANIFEST,)


def build_registry(manifests: tuple[ModuleManifest, ...] | None = None) -> ModuleRegistry:
    """Build the registry from the approved built-in manifests (or from ``manifests``, for tests). Raises ``RegistryError`` if they do not fit."""
    return ModuleRegistry.build(BUILTIN_MODULES if manifests is None else manifests)
