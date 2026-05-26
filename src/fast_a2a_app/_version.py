"""Single source of truth for the package version.

Hatchling reads ``__version__`` from this file at build time (see
``[tool.hatch.version]`` in ``pyproject.toml``) and the UI footer imports
it directly, so editing the constant below propagates to both the
installed metadata and the chat UI without further bookkeeping.
"""
__version__ = "0.6.12"
