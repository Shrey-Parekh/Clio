"""Put CUDA's DLLs where Windows can find them, for CTranslate2 (STT) and ONNX
Runtime (TTS).

The nvidia-*-cu12 pip packages ship the DLLs but don't add them to the search
path, and lazy CUDA init inside compiled code ignores os.add_dll_directory — only
a PATH prepend works, and it must happen before the first session is created.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from clio.core.logging import get_logger

log = get_logger("clio.core.cuda")

_registered = False


def ensure_cuda_dlls_on_path() -> None:
    """No-op off Windows, without the packages (CPU-only), or once applied —
    safe to call from every lazy loader."""
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
