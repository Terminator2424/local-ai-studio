"""Detección de hardware y ajuste automático de Ollama (modelo, contexto y capas en GPU).

La app se adapta sola al PC donde corre:
  - GPU NVIDIA: VRAM total/libre con NVML (nvidia-ml-py) o, si no está, con `nvidia-smi`.
  - Sin NVIDIA: se detecta si hay GPU integrada/AMD (Ollama la tratará como CPU) o solo CPU.
  - Se elige modelo, `num_ctx` y `num_gpu` según la memoria disponible, sin tocar tu config.yaml en disco.
Se puede desactivar con `hardware.auto: false` en config.yaml.
"""
from __future__ import annotations

import copy
import ctypes
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger("resumidor")

# Tamaño aproximado (GB, cuantización Q4) y nº de capas de los modelos conocidos
MODEL_INFO = {
    "qwen2.5:3b": (1.9, 36), "qwen2.5:7b": (4.7, 28), "qwen2.5:14b": (9.0, 48), "qwen2.5:32b": (20.0, 64),
    "qwen2.5vl:3b": (3.2, 36), "qwen2.5vl:7b": (6.0, 28),
}
# KV-cache aproximada por token (MB): 2 · capas · cabezas KV · dim · 2 bytes
KV_MB_PER_TOKEN = {"qwen2.5:3b": 0.036, "qwen2.5:7b": 0.057, "qwen2.5:14b": 0.19, "qwen2.5:32b": 0.25,
                   "qwen2.5vl:3b": 0.036, "qwen2.5vl:7b": 0.057}
OVERHEAD_GB = 0.9       # CUDA, ventanas del sistema, etc.


@dataclass
class HardwareInfo:
    gpu_name: str = "—"
    vendor: str = "cpu"                 # nvidia | integrated | cpu
    vram_total_gb: float = 0.0
    vram_free_gb: float = 0.0
    ram_gb: float = 0.0
    source: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class AutoConfig:
    text_model: str
    vision_model: str
    text_num_ctx: int
    vision_num_ctx: int
    text_num_gpu: int | None            # None = automático de Ollama
    vision_num_gpu: int | None
    reasons: list[str] = field(default_factory=list)


# --------------------------------------------------------------- detección
def _ram_gb() -> float:
    try:
        class MEMSTATUS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMSTATUS()
        st.dwLength = ctypes.sizeof(MEMSTATUS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return round(st.ullTotalPhys / 1024 ** 3, 1)
    except Exception:  # noqa: BLE001
        try:
            return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3, 1)
        except Exception:  # noqa: BLE001
            return 0.0


def _nvml() -> HardwareInfo | None:
    try:
        import pynvml  # nvidia-ml-py
        pynvml.nvmlInit()
        try:
            if pynvml.nvmlDeviceGetCount() < 1:
                return None
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(h)
            name = name.decode() if isinstance(name, bytes) else name
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            return HardwareInfo(name, "nvidia", round(mem.total / 1024 ** 3, 2), round(mem.free / 1024 ** 3, 2),
                                source="NVML")
        finally:
            pynvml.nvmlShutdown()
    except Exception as exc:  # noqa: BLE001
        log.info("NVML no disponible: %s", exc)
        return None


def _nvidia_smi() -> HardwareInfo | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if out.returncode != 0 or not out.stdout.strip():
            return None
        name, total, free = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        return HardwareInfo(name, "nvidia", round(float(total) / 1024, 2), round(float(free) / 1024, 2), source="nvidia-smi")
    except Exception as exc:  # noqa: BLE001
        log.info("nvidia-smi no disponible: %s", exc)
        return None


def _other_gpu() -> str:
    """Nombre de la GPU no NVIDIA (integrada/AMD) en Windows, si existe."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
            capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        names = [n.strip() for n in out.stdout.splitlines() if n.strip()]
        return names[0] if names else ""
    except Exception:  # noqa: BLE001
        return ""


_cache: HardwareInfo | None = None


def detect(force: bool = False) -> HardwareInfo:
    global _cache
    if _cache is not None and not force:
        return _cache
    info = _nvml() or _nvidia_smi()
    if info is None:
        other = _other_gpu()
        if other:
            info = HardwareInfo(other, "integrated", source="WMI")
            info.notes.append("GPU no NVIDIA: Ollama usará la CPU (la aceleración AMD/Intel depende de tu instalación).")
        else:
            info = HardwareInfo("Sin GPU dedicada", "cpu", source="—")
    info.ram_gb = _ram_gb()
    _cache = info
    return info


# --------------------------------------------------------------- decisión
def _fit_layers(model: str, ctx: int, free_gb: float) -> int | None:
    """Capas que caben en la VRAM libre; None si caben todas (Ollama decide)."""
    size_gb, layers = MODEL_INFO.get(model, (5.0, 32))
    kv_gb = KV_MB_PER_TOKEN.get(model, 0.1) * ctx / 1024
    avail = free_gb - OVERHEAD_GB
    if size_gb + kv_gb <= avail:
        return None
    if avail <= kv_gb * 0.5:
        return 0
    frac = max(0.0, (avail - kv_gb) / size_gb)
    fit = max(0, min(layers - 1, int(layers * frac)))
    # Si caben casi todas las capas, Ollama ya reparte bien por sí solo: solo se fuerza num_gpu cuando la VRAM es de verdad escasa
    return None if fit >= 0.6 * layers else fit


def ollama_has_models_loaded(cfg: dict) -> bool:
    """Si Ollama ya tiene modelos en VRAM, la VRAM «libre» que reporta la GPU no es representativa."""
    try:
        import ollama
        return bool(ollama.Client(host=cfg["ollama"]["host"]).ps().models)
    except Exception:  # noqa: BLE001
        return False


def effective_free_gb(info: HardwareInfo, cfg: dict) -> float:
    """VRAM de la que puede disponer Ollama: la libre real, o (total − reserva del escritorio) si Ollama ya
    ocupa parte de ella con modelos cargados."""
    if info.vendor != "nvidia":
        return 0.0
    if ollama_has_models_loaded(cfg):
        return max(0.0, info.vram_total_gb - 1.2)
    return info.vram_free_gb


def decide(info: HardwareInfo, installed: set[str], cfg: dict, free_gb: float | None = None) -> AutoConfig:
    hcfg = cfg.get("hardware", {})
    base_text = cfg["ollama"]["text_model"]
    base_vis = cfg["ollama"]["vision_model"]
    reasons: list[str] = []
    vram = info.vram_total_gb if info.vendor == "nvidia" else 0.0
    free = (free_gb if free_gb is not None else info.vram_free_gb) if info.vendor == "nvidia" else 0.0

    # --- modelo de texto
    text_model = base_text
    big = hcfg.get("large_model", "qwen2.5:14b")
    small = hcfg.get("small_model", "qwen2.5:3b")
    if vram >= float(hcfg.get("large_model_min_vram_gb", 12)):
        if any(m == big or m.startswith(big + "-") for m in installed):
            text_model = big
            reasons.append(f"VRAM {vram:.0f} GB ≥ 12 GB → modelo grande {big}")
        else:
            reasons.append(f"VRAM {vram:.0f} GB permite {big}, pero no está instalado (ollama pull {big}); se usa {base_text}")
    elif info.vendor != "nvidia" and info.ram_gb and info.ram_gb < 8:
        if any(m == small for m in installed):
            text_model = small
        reasons.append(f"Sin GPU y {info.ram_gb:.0f} GB de RAM: se recomienda {small}")

    # --- contexto
    if info.vendor != "nvidia":
        ctx = 4096
        reasons.append("Sin GPU NVIDIA → num_ctx 4096 (CPU)")
    elif vram < 4:
        ctx = 2048
    elif vram < 6:
        ctx = 4096
    elif vram < 8:
        ctx = 8192
    elif vram < 12:
        ctx = 16384
    elif text_model == big:
        ctx = 8192 if vram < 16 else 16384        # el 14B ocupa ~9 GB de pesos
    else:
        ctx = 16384
    if info.vendor == "nvidia":
        reasons.append(f"VRAM {vram:.1f} GB → num_ctx {ctx}")
    ctx = int(min(ctx, cfg["ollama"]["text_options"].get("num_ctx", 16384))) if hcfg.get("never_exceed_config_ctx", True) else ctx
    vctx = min(ctx, int(cfg["ollama"]["vision_options"].get("num_ctx", 8192)))

    # --- capas en GPU
    if info.vendor == "nvidia":
        t_gpu = _fit_layers(text_model, ctx, free or vram)
        v_gpu = _fit_layers(base_vis, vctx, free or vram)
        if t_gpu is not None:
            reasons.append(f"VRAM libre {free:.1f} GB insuficiente para {text_model}: num_gpu={t_gpu} (el resto en CPU)")
        if v_gpu is not None:
            reasons.append(f"Modelo de visión: num_gpu={v_gpu} (el resto en CPU)")
    else:
        t_gpu, v_gpu = 0, 0
    return AutoConfig(text_model, base_vis, ctx, vctx, t_gpu, v_gpu, reasons)


def apply_auto_config(cfg: dict, installed: set[str] | None = None) -> dict:
    """Devuelve una copia de cfg ajustada al hardware (con cfg['_hardware'] para el panel de diagnóstico).
    No escribe nada en disco: tu config.yaml se respeta y puedes desactivarlo con hardware.auto: false."""
    cfg = copy.deepcopy(cfg)
    info = detect()
    cfg["_hardware"] = {"info": info.__dict__, "auto": False, "reasons": []}
    if not cfg.get("hardware", {}).get("auto", True):
        cfg["_hardware"]["reasons"] = ["Ajuste automático desactivado (hardware.auto: false)"]
        return cfg
    auto = decide(info, installed or set(), cfg, effective_free_gb(info, cfg))
    cfg["ollama"]["text_model"] = auto.text_model
    cfg["ollama"]["text_options"]["num_ctx"] = auto.text_num_ctx
    cfg["ollama"]["vision_options"]["num_ctx"] = auto.vision_num_ctx
    for key, val in (("text_options", auto.text_num_gpu), ("vision_options", auto.vision_num_gpu)):
        if val is None:
            cfg["ollama"][key].pop("num_gpu", None)
        else:
            cfg["ollama"][key]["num_gpu"] = val
    cfg["_hardware"].update(auto=True, reasons=auto.reasons, applied={
        "text_model": auto.text_model, "vision_model": auto.vision_model, "text_num_ctx": auto.text_num_ctx,
        "vision_num_ctx": auto.vision_num_ctx, "text_num_gpu": auto.text_num_gpu, "vision_num_gpu": auto.vision_num_gpu})
    return cfg


def describe(cfg: dict) -> str:
    """Texto para el panel de Diagnóstico de la interfaz."""
    hw = cfg.get("_hardware")
    if not hw:
        return "Hardware sin detectar"
    i = hw["info"]
    lines = [f"GPU: {i['gpu_name']}"]
    if i["vendor"] == "nvidia":
        lines.append(f"VRAM: {i['vram_total_gb']:.1f} GB (libres {i['vram_free_gb']:.1f} GB)")
    else:
        lines.append("VRAM: — (se usará CPU)")
    lines.append(f"RAM: {i['ram_gb']:.0f} GB")
    a = hw.get("applied")
    if a:
        lines.append(f"Modelo: {a['text_model']}")
        lines.append(f"num_ctx: {a['text_num_ctx']} · num_gpu: {'auto' if a['text_num_gpu'] is None else a['text_num_gpu']}")
    for r in hw.get("reasons", []):
        lines.append(f"• {r}")
    return "\n".join(lines)


def installed_models(cfg: dict) -> set[str]:
    try:
        import ollama
        client = ollama.Client(host=cfg["ollama"]["host"])
        return {m.model for m in client.list().models}
    except Exception:  # noqa: BLE001
        return set()


if __name__ == "__main__":     # python -m src.hardware_detector
    import yaml
    from .utils import CONFIG_PATH
    c = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8"))
    print(describe(apply_auto_config(c, installed_models(c))))
