"""Política de acceso del asistente con herramientas: qué carpetas puede tocar y cómo se valida cada ruta.

Regla de oro: el modelo NUNCA decide una ruta por sí mismo fuera de las carpetas permitidas. Cada ruta que propone se
resuelve a su ruta real (siguiendo «..», enlaces simbólicos y uniones) y solo se acepta si cae dentro de una carpeta
permitida. Se rechazan nombres reservados de Windows, flujos alternativos de datos («archivo.txt:oculto») y rutas UNC.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..core.paths import BASE_DIR

WORKSPACE = BASE_DIR / "workspace"          # carpeta de trabajo por defecto: lectura y escritura
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Archivos que no se leen aunque estén dentro de una carpeta permitida (secretos y credenciales típicas)
_SEP = r"[\\/]"        # separador de ruta de Windows o Unix
_SENSITIVE_DIR = re.compile(rf"(?i)(^|{_SEP})(\.ssh|\.gnupg|\.aws|\.azure|\.kube|\.docker|\.config{_SEP}gcloud|"
                            rf"AppData{_SEP}Roaming{_SEP}Microsoft{_SEP}(Credentials|Protect|Vault))({_SEP}|$)")
_SENSITIVE_FILE = re.compile(rf"(?i)(^|{_SEP})(\.git-credentials|\.netrc|id_rsa|id_ed25519|id_ecdsa|credenciales?\.\w+|"
                             r"passwords?\.\w+|contrase[ñn]as?\.\w+|secrets?\.\w+|\.env(\.\w+)?|NTUSER\.DAT.*|"
                             r"\.claude\.json.*|.*\.(pem|key|pfx|p12|kdbx|ovpn))$")
MAX_READ = 200_000          # bytes que se devuelven al modelo de un archivo
MAX_WRITE = 500_000


class ToolDenied(Exception):
    """La acción pedida no está permitida por la política (se informa al modelo y al usuario)."""


@dataclass
class Policy:
    read_roots: list[Path] = field(default_factory=lambda: [WORKSPACE])
    write_roots: list[Path] = field(default_factory=lambda: [WORKSPACE])

    def __post_init__(self):
        for r in self.read_roots + self.write_roots:
            r.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- rutas
    def resolve(self, raw: str, *, write: bool = False, must_exist: bool = False) -> Path:
        roots = self.write_roots if write else self.read_roots
        if not isinstance(raw, str) or not raw.strip():
            raise ToolDenied("Ruta vacía.")
        raw = raw.strip().strip('"')
        if "\x00" in raw or len(raw) > 400:
            raise ToolDenied("Ruta no válida.")
        if raw.startswith(("\\\\", "//")):
            raise ToolDenied("Las rutas de red (UNC) no están permitidas.")
        # «C:» solo puede aparecer como letra de unidad al principio; cualquier otro «:» es un flujo alternativo de datos
        if ":" in raw[2:] or (":" in raw[:2] and not re.match(r"^[A-Za-z]:", raw)):
            raise ToolDenied("La ruta contiene ':' (flujos alternativos de datos no permitidos).")
        p = Path(raw)
        if not p.is_absolute():
            p = roots[0] / p
        for part in p.parts:
            stem = part.rstrip(". ").split(".")[0].upper()
            if stem in _RESERVED:
                raise ToolDenied(f"Nombre reservado de Windows: {part}")
        real = Path(os.path.realpath(p))
        if not any(_inside(real, Path(os.path.realpath(r))) for r in roots):
            names = ", ".join(str(r) for r in roots)
            raise ToolDenied(f"Fuera de las carpetas permitidas ({names}).")
        if _SENSITIVE_DIR.search(str(real)) or _SENSITIVE_FILE.search(str(real)):
            raise ToolDenied("Archivo sensible (claves, credenciales o secretos): no se accede a él.")
        if must_exist and not real.exists():
            raise ToolDenied(f"No existe: {real}")
        return real

    def describe(self) -> str:
        return ("Lectura: " + "; ".join(map(str, self.read_roots)) + "\nEscritura: " + "; ".join(map(str, self.write_roots)))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_new_root(raw: str) -> Path:
    """Valida una carpeta que el usuario quiere añadir como lectura. No se admiten raíces de unidad, el sistema ni
    carpetas que contengan credenciales (el perfil completo contiene .ssh, AppData, etc.: se elige una subcarpeta)."""
    p = Path(os.path.realpath(raw))
    if not p.is_dir():
        raise ToolDenied("No es una carpeta.")
    if p.parent == p:             # raíz de una unidad (C:\)
        raise ToolDenied("No se permite una unidad completa.")
    home = Path(os.path.realpath(Path.home()))
    system = [os.environ.get("SYSTEMROOT", r"C:\Windows"), os.environ.get("PROGRAMFILES", r"C:\Program Files"),
              os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), os.environ.get("PROGRAMDATA", r"C:\ProgramData")]
    for s in system:
        if _inside(p, Path(os.path.realpath(s))):
            raise ToolDenied("No se permiten carpetas del sistema ni de programas.")
    if p == home or _inside(home, p):
        raise ToolDenied("No se permite tu carpeta de usuario completa (contiene claves y datos de aplicaciones): "
                         "elige una subcarpeta como Documentos.")
    if _SENSITIVE_DIR.search(str(p)) or _inside(p, home / "AppData"):
        raise ToolDenied("Esa carpeta contiene datos sensibles de aplicaciones o claves.")
    return p
