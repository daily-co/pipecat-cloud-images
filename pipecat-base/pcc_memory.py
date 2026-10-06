"""Hand a finished session's memory back to the OS before the pod takes another.

A warm pod runs every session in this one process, and the pod's memory metric
and its limit are the process's. Two things make that memory climb from one
session to the next even though nothing outlives its session:

* A finished session is held in reference cycles. Pipecat's processors link to
  each other both ways, every BaseObject's event handlers point back at their
  owner, and SDKs such as botocore build cyclic config chains. Reference
  counting cannot free a cycle; only a full collection can, and Python runs one
  only every few sessions, so several dead sessions pile up first.
* When they are freed, glibc keeps the memory. It is scattered through the
  heap, and glibc returns only the free space at its top on its own, so RSS
  ratchets up while the memory actually in use stays flat.

release_session_memory() runs a full collection and then malloc_trim(0), which
returns free pages from anywhere in the heap. app.py runs it after bot()
returns and before the pod is freed, once a few event-loop turns have let the
session's teardown drop its last references. It is skipped while another
session is running in this process: a full collection holds the GIL for tens
of milliseconds, and that session's audio would stall.

A session whose code leaves a timer or task scheduled that references it stays
alive past bot(). Its release then frees little, and the next session's
release frees both, so such a session is carried over once and never piles up.

malloc_trim is glibc's; where it is missing (musl, a non-Linux dev machine) only
the collection runs. Set PCC_RELEASE_SESSION_MEMORY=false to turn this off.
"""

import ctypes
import ctypes.util
import gc
import os
import time
from typing import Callable, Optional

from loguru import logger

ENABLED_ENV = "PCC_RELEASE_SESSION_MEMORY"


def _load_malloc_trim() -> Optional[Callable[[int], int]]:
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        malloc_trim = libc.malloc_trim
    except (OSError, AttributeError):
        return None
    malloc_trim.argtypes = [ctypes.c_size_t]
    malloc_trim.restype = ctypes.c_int
    return malloc_trim


_malloc_trim = _load_malloc_trim()


def _read_enabled() -> bool:
    """PCC_RELEASE_SESSION_MEMORY: true (the default) or false."""
    value = os.environ.get(ENABLED_ENV, "true").strip().lower()
    if value not in ("true", "false"):
        logger.error(f"{ENABLED_ENV} must be true or false, not {value!r}; leaving it on.")
        return True
    return value == "true"


ENABLED = _read_enabled()


def release_session_memory() -> None:
    """Collect the finished session's cycles, then return freed heap to the OS.

    Synchronous on purpose: gc.collect() holds the GIL whichever thread runs it,
    so a worker thread would not spare the event loop the pause, and it would
    run the session's finalizers off the loop thread.
    """
    if not ENABLED:
        return
    started = time.perf_counter()
    collected = gc.collect()
    message = f"Released session memory: collected {collected} objects in {_ms_since(started)}"
    if _malloc_trim is not None:
        trim_started = time.perf_counter()
        returned = bool(_malloc_trim(0))
        message += f", malloc_trim returned memory: {returned} in {_ms_since(trim_started)}"
    logger.debug(message)


def _ms_since(started: float) -> str:
    return f"{(time.perf_counter() - started) * 1000:.0f} ms"
