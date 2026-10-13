## Qué cambia y por qué

<!-- Una o dos frases. Si corrige un fallo, explica cómo se reproducía. -->

## Comprobaciones

- [ ] `python -m ruff check .` y `python -m mypy local_ai_studio` sin errores
- [ ] `python -m unittest discover -s tests` en verde (con pruebas nuevas si cambia el comportamiento)
- [ ] Si toca la extracción o los prompts del resumen: subí `EXTRACT_VERSION` / `PIPELINE_VERSION`
- [ ] Si toca seguridad (red, rutas, sandbox, descargas): revisé `SECURITY.md` y corrí `bandit`
- [ ] Si toca la interfaz: adjunto una captura (clara y oscura)
