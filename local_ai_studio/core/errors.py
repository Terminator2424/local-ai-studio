"""Excepciones propias de la aplicación."""
from __future__ import annotations


class Cancelled(Exception):
    """El usuario canceló el proceso."""


class PDFError(Exception):
    """Error al abrir/leer un PDF (corrupto, cifrado, sin texto...)."""
