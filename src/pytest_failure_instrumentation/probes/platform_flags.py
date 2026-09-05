"""Which platform this is, named once so nothing else tests ``sys.platform``.

Coverage differs by platform in ways that are not cosmetic, and every probe
that degrades needs to say which of these it degraded on.
"""

from __future__ import annotations

import platform
import sys
from functools import lru_cache

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


@lru_cache(maxsize=1)
def platform_description() -> str:
    """Describe the OS without querying unused Windows CPU information.

    platform.platform() unpacks uname's processor field before selecting its
    Windows branch. On Python 3.12 that starts a WMI CPU query even though the
    resulting Windows string uses only OS fields. win32_ver supplies those
    fields directly, retaining its OS-version and service-pack fallbacks.
    """
    if not IS_WINDOWS:
        return platform.platform()
    release, version, service_pack, _build_type = platform.win32_ver()
    return "-".join(part.replace(" ", "_") for part in (
        "Windows", release, version, service_pack,
    ) if part)
