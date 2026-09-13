"""_no_wmi.py — make platform.system()/win32_ver() immune to a hung WMI service.

Python 3.12's platform module queries WMI (Win32_OperatingSystem) the first
time anything asks for the Windows version. onnxruntime, torch, and friends
all call platform.system() at import time. When the WMI service is wedged —
as it was during the 2026-06-10 commit-exhaustion crash — that query blocks
INDEFINITELY, turning every hook/worker spawn that imports fastembed or
onnxruntime into a stuck 660MB zombie (observed: prefetch.py PIDs frozen for
100+ minutes inside platform._wmi_query).

CPython's own design falls back to the registry/getwindowsversion path when
_wmi_query raises OSError, so we force that fallback unconditionally. The
only cost is a marginally less precise os-version string. Import this module
BEFORE anything that might import onnxruntime/torch/transformers.
"""
import platform


def _wmi_query_disabled(*args, **kwargs):
    raise OSError("WMI disabled by _no_wmi.py (hung-WMI guard, 2026-06-10)")


try:
    platform._wmi_query = _wmi_query_disabled
except Exception:
    pass
