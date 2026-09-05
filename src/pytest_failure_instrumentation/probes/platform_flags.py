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
    """Describe the OS without a WMI query in every worker's startup.

    Even win32_ver() queries WMI on Python 3.12. The interpreter already
    exposes the kernel version and product type needed for diagnostic logs;
    use those rather than resolving a marketing release name.
    """
    if not IS_WINDOWS:
        return platform.platform()
    try:
        info = sys.getwindowsversion()  # type: ignore[attr-defined]
        version = ".".join(str(part) for part in info.platform_version)
        release = {1: "Workstation", 2: "DomainController", 3: "Server"}.get(info.product_type, "")
        service_pack = info.service_pack
    except (AttributeError, OSError):
        release, version, service_pack, _build_type = platform.win32_ver()
    return "-".join(part.replace(" ", "_") for part in (
        "Windows", release, version, service_pack,
    ) if part)
