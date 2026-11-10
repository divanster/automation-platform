"""Workflow packages: plain-Python business logic that is independent of Django.

A workflow package owns its result schema and validation rules
(docs/architecture/overview.md, section 7). Code here must not import Django,
touch the filesystem or network, or depend on any AI provider SDK.
"""
