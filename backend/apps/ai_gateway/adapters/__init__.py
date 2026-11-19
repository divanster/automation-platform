"""AI provider adapters. Each module here translates the gateway's ``ProviderCall`` for ONE provider kind.

AI-1a contains only the deterministic TEST adapter. Adapters import the gateway contract and the standard library, and nothing else: no
business module, no workflow package, no Django. A business module never imports from here; adapters are chosen only in
``config/ai_gateway.py``.
"""
