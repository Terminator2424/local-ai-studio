"""Copiar una imagen al portapapeles de Windows (formato CF_DIB) sin dependencias externas."""
from __future__ import annotations

import io
import sys


def dib_bytes(img) -> bytes:
    """Imagen → bytes DIB (un BMP sin la cabecera de archivo de 14 bytes)."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    return buf.getvalue()[14:]


def copy_image(img) -> bool:
    """Pone la imagen en el portapapeles. Devuelve False si no es Windows o si falla."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    data = dib_bytes(img)
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    GMEM_MOVEABLE, CF_DIB = 0x0002, 8

    handle = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    ptr = k32.GlobalLock(handle)
    if not ptr:
        k32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, data, len(data))
    k32.GlobalUnlock(handle)
    if not u32.OpenClipboard(None):
        k32.GlobalFree(handle)
        return False
    try:
        u32.EmptyClipboard()
        if not u32.SetClipboardData(CF_DIB, handle):
            k32.GlobalFree(handle)
            return False
        return True            # el sistema es ahora dueño de la memoria
    finally:
        u32.CloseClipboard()
