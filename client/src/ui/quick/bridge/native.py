"""libarkcontrols.so (client/native/arkcontrols), loaded once with ctypes into the
running Qt: the ark.controls value type, and the native side of AsyncImageProvider."""

from __future__ import annotations

import ctypes
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "native" / "libarkcontrols.so"
_library: ctypes.CDLL | None = None


def library() -> ctypes.CDLL:
    global _library
    if _library is None:
        if not PATH.exists():
            raise RuntimeError(f"{PATH} is missing: run install.py, or build client/native/arkcontrols "
                               "(see its CMakeLists.txt)")
        lib = ctypes.CDLL(str(PATH))         # CDLL calls release the GIL
        lib.rm_register_ark_controls.argtypes, lib.rm_register_ark_controls.restype = [], None
        lib.rm_add_image_provider.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
        lib.rm_add_image_provider.restype = ctypes.c_bool
        lib.rm_deliver_image.argtypes, lib.rm_deliver_image.restype = [ctypes.c_int, ctypes.c_void_p], ctypes.c_bool
        _library = lib
    return _library
