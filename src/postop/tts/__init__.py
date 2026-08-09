"""Sintesis de voz: dos backends intercambiables.

  piper (por defecto)  local, 65-300 ms al primer audio, funciona sin internet.
  edge                 voces colombianas neuronales, ~665 ms, requiere red.

El local es el predeterminado a proposito: la promesa de que nada del paciente
sale de la maquina pesa mas que la naturalidad de la voz. Ver tts/edge.py para
el analisis completo del intercambio.
"""

from __future__ import annotations

from pathlib import Path

VOZ_PIPER_POR_DEFECTO = "es_MX-claude-high"


def crear_sintetizador(backend: str, voz: str, *, modelos: Path, cache_dir: Path):
    """Devuelve el sintetizador configurado, replegandose a piper si hace falta.

    El repliegue no es un adorno: el backend neuronal necesita red, y una sesion
    evaluada en vivo sin voz falla la compuerta G4. Prefiero una voz peor a
    ninguna voz.
    """
    if backend == "edge":
        try:
            from postop.tts.edge import SintetizadorEdge

            sintetizador = SintetizadorEdge(voz, cache_dir=cache_dir)
            # Prueba real: construir el objeto no basta. El servicio de Microsoft
            # puede rechazar la conexion (403 desde dentro de Docker, red sin
            # salida, bloqueo por region), y eso solo se descubre al sintetizar.
            # Si se dejara para el primer turno, la aplicacion arrancaria y la
            # llamada del jurado se quedaria muda.
            if not any(sintetizador.sintetizar("prueba")):
                raise RuntimeError("el servicio devolvió audio vacío")
            return sintetizador
        except Exception as exc:  # noqa: BLE001
            print(f"  aviso: backend de voz 'edge' no disponible ({type(exc).__name__}); "
                  f"se repliega a piper")
            voz = VOZ_PIPER_POR_DEFECTO

    from postop.tts.voz import SintetizadorVoz

    ruta = modelos / "piper" / f"{voz}.onnx"
    if not ruta.exists():
        ruta = modelos / "piper" / f"{VOZ_PIPER_POR_DEFECTO}.onnx"
    if not ruta.exists():
        return None
    return SintetizadorVoz(ruta, cache_dir=cache_dir)
