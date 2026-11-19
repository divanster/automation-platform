"""The composition root of the audit vocabulary: turns a validated module registry into the vocabulary events are checked against.

Standard library, ``apps.modules``, ``apps.audit.events``/``vocabulary`` (pure, no models) and ``config.modules`` only. It imports no module
package, no business code and no Django, and it reads nothing from the database, the environment or the file system, so importing it is safe
before Django's app registry is ready. Core packages never import this file; a test pins that.

* ``build_audit_vocabulary(registry=None)`` is a pure function: Core's vocabulary plus every ``AuditActionDeclaration`` of the registered
  modules, with the namespace owners and the reserved namespaces the registry knows. ``registry=None`` means the built-in modules
  (``config.modules.BUILTIN_MODULES``: Request Intake since MOD-2. It declares no audit action, because every action it emits is Core's, so the result still equals Core's vocabulary).
* ``install_audit_vocabulary(registry=None)`` builds and installs it (``apps.audit.events.install_vocabulary``). It is idempotent, refuses a
  conflicting second installation, and is not called by any settings module, app config or entry point yet: with no module registered
  Core's own vocabulary is already the installed default, and the first module (MOD-2) decides where the call belongs (for example next to
  the worker's composition in ``runworker`` and the API's start-up).

A module may emit Core events through Core's services; it cannot redefine them. Declaring an action is a naming rule enforced in Python; it
is not a sandbox and not database protection (``apps.audit.vocabulary``).
"""

from apps.audit.events import CORE_VOCABULARY, install_vocabulary
from apps.audit.vocabulary import AuditVocabulary, ModuleAuditAction
from apps.modules.manifest import RESERVED_NAMESPACES
from apps.modules.registry import ModuleRegistry
from config.modules import build_registry


def build_audit_vocabulary(registry: ModuleRegistry | None = None) -> AuditVocabulary:
    """Core's audit vocabulary plus the actions the registry's modules declare. Pure and deterministic; ``AuditVocabularyError`` if they do not fit."""
    if registry is None:
        registry = build_registry()
    if not isinstance(registry, ModuleRegistry):
        raise TypeError("registry must be an apps.modules.registry.ModuleRegistry")
    declarations = tuple(
        ModuleAuditAction(manifest.module_id, declaration.action, declaration.detail_keys)
        for manifest in registry.modules
        for declaration in manifest.audit_actions
    )
    return CORE_VOCABULARY.with_modules(declarations, registry.namespace_owners, RESERVED_NAMESPACES)


def install_audit_vocabulary(registry: ModuleRegistry | None = None) -> AuditVocabulary:
    """Build the vocabulary and install it for this process. Safe to call again with the same registry; a different vocabulary is refused."""
    return install_vocabulary(build_audit_vocabulary(registry))
