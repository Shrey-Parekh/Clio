"""Putting CUDA's DLLs where Windows can actually find them.

Two different libraries need this and neither can do it for itself:
faster-whisper's CTranslate2 backend (STT) and ONNX Runtime's CUDA provider
(Kokoro TTS). The pip packages that ship the DLLs - nvidia-cublas-cu12,
nvidia-cudnn-cu12 - don't put them on the search path, and lazy CUDA init deep
inside compiled code doesn't respect os.add_dll_directory. Only a plain PATH
prepend works, and it has to happen before the first session is created.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from clio.core.logging import get_logger

log = get_logger("clio.core.cuda")

_registered = False


def ensure_cuda_dlls_on_path() -> None:
    """No-op outside Windows, if the packages aren't installed (CPU-only
    setups), or if already applied - safe to call from every lazy loader."""
    global _registered
    if _registered or sys.platform != "win32":
        return

    site_packages = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    dll_dirs = [site_packages / "cublas" / "bin", site_packages / "cudnn" / "bin"]
    existing = [str(d) for d in dll_dirs if d.is_dir()]
    if existing:
        os.environ["PATH"] = os.pathsep.join(existing) + os.pathsep + os.environ.get("PATH", "")
        log.debug("CUDA DLL directories added to PATH", extra={"extra_fields": {"dirs": existing}})
    _registered = True
