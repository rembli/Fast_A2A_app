"""fast_a2a_app.ui — Self-contained A2A chat UI (Starlette ASGI app)."""
from .route import a2a_ui, build_a2a_ui

__all__ = ["a2a_ui", "build_a2a_ui"]
