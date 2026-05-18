"""
fast_a2a_app.server.commons — best-practice helpers for common agent patterns.

The ``commons`` package is strictly downstream of ``fast_a2a_app`` core: it
imports from the framework but is never imported by it. Modules here ship
reusable building blocks for patterns the framework deliberately leaves to
user code (so the core stays small and orchestrator-agnostic), but that
nearly every non-trivial agent ends up reinventing.

Available modules:

* :mod:`fast_a2a_app.server.commons.config` — conversation-scoped parameters
  driven by a ``/set`` slash-command wizard, recovered from A2A history
  each turn. See its module docstring for the full pattern.
* :mod:`fast_a2a_app.server.commons.uploads` — file-attachment parsing
  helpers for the current turn (``context.message.parts``) and prior turns
  (``context.related_tasks``). See its module docstring for the wiring.
"""
