"""Carga de config.yaml con valores por defecto: una clave que falta nunca rompe la aplicación."""
from __future__ import annotations

import copy
import logging

import yaml

from .ollama_client import DEFAULT_HOST, enforce_local_host
from .paths import CONFIG_EXAMPLE, CONFIG_PATH

log = logging.getLogger(__name__)


def _merge(defaults: dict, user: dict) -> dict:
    """Mezcla recursiva: lo que escribe el usuario manda; lo que falta se toma de config.example.yaml."""
    out = copy.deepcopy(defaults)
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} no es un mapa YAML válido.")
    return data


def load_config(auto_hardware: bool = True) -> dict:
    """Carga config.yaml (si no existe, se crea desde config.example.yaml) y aplica el ajuste automático al hardware."""
    if not CONFIG_PATH.exists() and CONFIG_EXAMPLE.exists():
        CONFIG_PATH.write_text(CONFIG_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    defaults = _read_yaml(CONFIG_EXAMPLE) if CONFIG_EXAMPLE.exists() else {}
    try:
        cfg = _merge(defaults, _read_yaml(CONFIG_PATH))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        log.warning("config.yaml ilegible (%s): se usan los valores por defecto", exc)
        cfg = defaults
    enforce_local_host(cfg.get("ollama", {}).get("host", DEFAULT_HOST))
    if auto_hardware:
        try:
            from .hardware_detector import apply_auto_config, installed_models
            cfg = apply_auto_config(cfg, installed_models(cfg))
        except Exception as exc:
            log.warning("No se pudo detectar el hardware (se usa config.yaml tal cual): %s", exc)
    return cfg
