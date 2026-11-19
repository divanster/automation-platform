"""AI provider adapters. Each module here translates the gateway's ``ProviderCall`` for ONE provider kind.

The deterministic TEST adapter (AI-1a) and the KPMG Workbench adapter (AI-1b). Adapters import the gateway contract and the standard library,
and nothing else: no business module, no workflow package, no Django, no socket (an HTTP adapter talks to an ``HttpTransport``). A business
module never imports from here; adapters are chosen only in a composition root: ``config/ai_gateway.py`` (empty) or, opt-in,
``config/ai_workbench.py``.
"""
