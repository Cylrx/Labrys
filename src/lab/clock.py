"""Suspend-inclusive elapsed time for session authorization."""

import ctypes
import sys
import time
from collections.abc import Callable

from lab.errors import LabError


def _source() -> Callable[[], float]:
    if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        return lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
    if sys.platform == "darwin":

        class Timebase(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        continuous = library.mach_continuous_time
        continuous.argtypes = []
        continuous.restype = ctypes.c_uint64
        timebase_info = library.mach_timebase_info
        timebase_info.argtypes = [ctypes.POINTER(Timebase)]
        timebase_info.restype = ctypes.c_int
        timebase = Timebase()
        if timebase_info(ctypes.byref(timebase)) != 0 or not timebase.denom or not timebase.numer:
            raise LabError("The suspend-inclusive session clock is unavailable.")
        scale = timebase.numer / timebase.denom / 1_000_000_000
        return lambda: continuous() * scale
    raise LabError("This platform has no supported suspend-inclusive session clock.")


_now: Callable[[], float] | None = None


def now() -> float:
    """Return elapsed seconds including suspended time, unaffected by wall-clock changes."""
    global _now
    if _now is None:
        _now = _source()
    return _now()
