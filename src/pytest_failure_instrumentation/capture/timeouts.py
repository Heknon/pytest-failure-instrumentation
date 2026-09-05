"""Effective terminating deadlines, read where per-test markers are available."""
from __future__ import annotations

from typing import Any


def effective(item: Any) -> list[dict[str, Any]]:
    deadlines: list[dict[str, Any]] = []
    config = item.config
    if config.pluginmanager.hasplugin("timeout"):
        try:
            from pytest_timeout import _get_item_settings

            settings = _get_item_settings(item)
            if settings.timeout is not None and settings.timeout > 0:
                deadlines.append({"source": "pytest-timeout", "seconds": settings.timeout,
                                  "scope": "call" if settings.func_only else "test",
                                  "method": settings.method})
        except (ImportError, AttributeError, TypeError, ValueError):
            pass  # Unknown plugin versions must not manufacture a deadline.
    try:
        duration = float(config.getini("faulthandler_timeout") or 0)
        terminates = config.getini("faulthandler_exit_on_timeout")
        if duration > 0 and terminates:
            deadlines.append({"source": "faulthandler_timeout", "seconds": duration,
                              "scope": "test", "method": "exit"})
    except (ValueError, KeyError, TypeError):
        pass
    return deadlines
