"""Ejecuta código Python de forma aislada, con varias capas (ninguna es perfecta por sí sola):

1. Proceso aparte, intérprete aislado (-I), carpeta temporal como directorio de trabajo y entorno mínimo.
2. Gancho de auditoría DENTRO del proceso hijo (PEP 578), instalado antes del código del usuario: bloquea red, DNS,
   procesos hijos, `ctypes`, cargas de bibliotecas y cualquier lectura o escritura fuera de la carpeta temporal y de la
   instalación de Python. No se puede desinstalar desde Python.
3. Windows Job Object: límite de memoria, UN solo proceso (no puede lanzar hijos) y muerte garantizada al cerrar.
4. Tiempo máximo y tope de salida.

LÍMITE HONESTO: no es una frontera de seguridad absoluta. El código malicioso muy elaborado podría evadir un gancho de
auditoría de Python. Por eso este módulo solo se usa con código que el usuario ha VISTO y APROBADO, y nunca ejecuta
nada por su cuenta. Para código realmente hostil haría falta una máquina virtual.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# El python.exe de un venv es un lanzador que crea un hijo; con "un solo proceso" se usa el intérprete base directamente
PYTHON = getattr(sys, "_base_executable", None) or sys.executable

GUARD = r'''
import sys, os, builtins
_SANDBOX = os.path.realpath(os.getcwd())
_ALLOW_READ = [_SANDBOX] + [os.path.realpath(p) for p in {sys.prefix, sys.base_prefix, sys.exec_prefix} if p]
_ALLOW_READ += [os.path.realpath(p) for p in sys.path if p and os.path.isdir(p) and os.path.realpath(p).lower().startswith(
    tuple(os.path.realpath(q).lower() for q in {sys.prefix, sys.base_prefix}))]
_DENY = {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
         "socket.sendto", "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile",
         "os.fork", "os.forkpty", "os.kill", "os.killpg", "ctypes.dlopen", "ctypes.dlsym", "ctypes.cdata", "ctypes.call_function",
         "ctypes.set_exception", "ctypes.addressof", "ctypes.create_unicode_buffer", "ctypes.create_string_buffer",
         "winreg.OpenKey", "winreg.CreateKey", "winreg.SetValue", "winreg.DeleteKey", "import.winreg",
         "webbrowser.open", "msvcrt.locking", "shutil.rmtree", "pty.spawn", "mmap.__new__"}
_WRITE_EVENTS = {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.utime",
                 "os.link", "os.symlink", "os.replace", "shutil.copyfile", "shutil.copytree", "shutil.move", "os.chdir"}
_BLOCK_IMPORT = {"ctypes", "_ctypes", "socket", "_socket", "ssl", "_ssl", "subprocess", "_winapi", "multiprocessing",
                 "winreg", "_winreg", "webbrowser", "smtplib", "ftplib", "telnetlib", "http", "urllib", "xmlrpc", "asyncio",
                 "pty", "msvcrt", "signal", "_posixsubprocess"}

def _inside(p, roots):
    try:
        p = os.path.realpath(p)
    except Exception:
        return False
    pl = os.path.normcase(p)
    return any(pl == os.path.normcase(r) or pl.startswith(os.path.normcase(r) + os.sep) for r in roots)

def _hook(event, args):
    if event in _DENY or any(event.startswith(d + ".") for d in _DENY):
        raise PermissionError("bloqueado por el sandbox: " + event)
    if event == "import" and args and args[0].split(".")[0] in _BLOCK_IMPORT:
        raise ImportError("módulo no permitido en el sandbox: " + args[0])
    if event == "open":
        path, mode = args[0], (args[1] or "r")
        if isinstance(path, int):
            return
        path = os.fsdecode(path)
        writing = any(c in str(mode) for c in "wax+")
        if not _inside(path, [_SANDBOX] if writing else _ALLOW_READ):
            raise PermissionError("acceso a archivos fuera del sandbox bloqueado: " + path)
    elif event in _WRITE_EVENTS:
        for a in args:
            if isinstance(a, (str, bytes, os.PathLike)):
                if not _inside(os.fsdecode(a), [_SANDBOX]):
                    raise PermissionError("modificación fuera del sandbox bloqueada: " + os.fsdecode(a))
    elif event in ("os.listdir", "os.scandir") and args and args[0] is not None and not isinstance(args[0], int):
        if not _inside(os.fsdecode(args[0]), _ALLOW_READ):
            raise PermissionError("listado fuera del sandbox bloqueado")

# Espera la señal del padre: el Job Object ya está aplicado antes de ejecutar nada del usuario
_go = os.path.join(_SANDBOX, ".go")
import time as _t
_t0 = _t.time()
while not os.path.exists(_go) and _t.time() - _t0 < 5:
    _t.sleep(0.003)
import runpy
# Módulos bloqueados que Python ya cargó al arrancar (winreg, _winapi…): sin esto la importación no dispararía el bloqueo
for _m in list(sys.modules):
    if _m.split(".")[0] in _BLOCK_IMPORT:
        del sys.modules[_m]
sys.addaudithook(_hook)
sys.argv = ["main.py"]
runpy.run_path(os.path.join(_SANDBOX, "main.py"), run_name="__main__")
'''


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    truncated: bool = False
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _make_job(memory_mb: int):
    """Job Object de Windows: memoria limitada, 1 proceso activo, muerte al cerrar. None si no es Windows."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "ro", "wo", "oo")]

    class EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class UI(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    ext = EXT()
    ext.BasicLimitInformation.LimitFlags = 0x2000 | 0x100 | 0x8      # KILL_ON_JOB_CLOSE | PROCESS_MEMORY | ACTIVE_PROCESS
    ext.BasicLimitInformation.ActiveProcessLimit = 1
    ext.ProcessMemoryLimit = memory_mb * 1024 * 1024
    ok1 = k32.SetInformationJobObject(job, 9, ctypes.byref(ext), ctypes.sizeof(ext))              # ExtendedLimitInformation
    ui = UI(0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x20 | 0x40)    # handles, escritorio, parámetros, apagado, clipboard…
    k32.SetInformationJobObject(job, 4, ctypes.byref(ui), ctypes.sizeof(ui))                       # UIRestrictions
    if not ok1:
        k32.CloseHandle(job)
        return None
    return job


def _assign(job, proc) -> bool:
    import ctypes
    k32 = ctypes.windll.kernel32
    return bool(k32.AssignProcessToJobObject(job, int(proc._handle)))


def run_python(code: str, timeout: float = 10.0, max_output: int = 20_000, memory_mb: int = 256) -> SandboxResult:
    """Ejecuta `code` en el sandbox. Nunca lanza por culpa del código del usuario: devuelve el resultado."""
    if len(code) > 100_000:
        return SandboxResult("", "El código es demasiado largo (máximo 100 KB).", 1)
    if getattr(sys, "frozen", False):          # empaquetado con PyInstaller: no hay intérprete de Python que aislar
        return SandboxResult("", "El sandbox no está disponible en la versión empaquetada (necesita Python instalado).", 1)
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="sandbox_") as tmp:
        d = Path(tmp)
        (d / "main.py").write_text(code, encoding="utf-8")
        (d / "guard.py").write_text(GUARD, encoding="utf-8")
        env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
               "TEMP": str(d), "TMP": str(d), "PATH": "", "NO_PROXY": "*", "PYTHONDONTWRITEBYTECODE": "1"}
        job = _make_job(memory_mb)
        proc = subprocess.Popen([PYTHON, "-I", "guard.py"], cwd=str(d), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if job is not None and not _assign(job, proc):
            proc.kill()
            return SandboxResult("", "No se pudo aplicar el aislamiento (Job Object); no se ejecuta el código.", 1)
        (d / ".go").write_text("1")
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            out, err = proc.communicate()
        finally:
            if job is not None:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(job)       # KILL_ON_JOB_CLOSE
    out_t, err_t = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    trunc = len(out_t) > max_output or len(err_t) > max_output
    err_t = err_t.replace(str(d), "<sandbox>")
    return SandboxResult(out_t[:max_output], err_t[:max_output], None if timed_out else proc.returncode, timed_out, trunc,
                         round(time.time() - t0, 2))
