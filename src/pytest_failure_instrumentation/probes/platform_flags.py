"""Which platform this is, named once so nothing else tests ``sys.platform``.

Coverage differs by platform in ways that are not cosmetic, and every probe
that degrades needs to say which of these it degraded on.
"""

from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def optional_psutil():
    """psutil if the host happens to have it; never a dependency."""
    try:
        import psutil

        return psutil
    except Exception:
        return None
