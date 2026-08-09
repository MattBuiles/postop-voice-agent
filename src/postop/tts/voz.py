"""Sintesis de voz con Piper.

Dos optimizaciones sostienen la latencia reportada, y ninguna es cosmetica:

1. **Pre-sintesis de las frases guionadas.** El agente dice texto fijo en la
   mayoria de sus turnos (saludo, las seis preguntas, las repreguntas, los
   cierres, los backchannels de silencio). Ese audio se genera una vez al
   arrancar y despues se sirve del cache: 0 ms de TTS en el turno.

   Medido en este equipo: el primer chunk de audio de una frase nueva tarda
   ~1.0 s. Pagarlo en cada turno haria imposible el objetivo de latencia; no
   pagarlo nunca en los turnos guionados es lo que lo vuelve alcanzable.

2. **Troceo por oracion.** Para el texto que si es generado, se sintetiza y se
   emite oracion por oracion, de modo que el paciente empieza a oir la primera
   mientras la segunda todavia se genera.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterator
from pathlib import Path

_FIN_ORACION = re.compile(r"(?<=[.!?…])\s+|(?<=\?)\s*")


class SintetizadorVoz:
    def __init__(self, ruta_modelo: Path, *, cache_dir: Path | None = None) -> None:
        from piper import PiperVoice

        self._voz = PiperVoice.load(str(ruta_modelo), str(ruta_modelo) + ".json")
        self.backend = "piper"
        self.voz = ruta_modelo.stem
        self.sample_rate: int = self._voz.config.sample_rate
        self._cache: dict[str, bytes] = {}
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ cache

    @staticmethod
    def _clave(texto: str) -> str:
        return hashlib.sha256(texto.strip().encode("utf-8")).hexdigest()[:20]

    def precalentar(self, frases: list[str]) -> int:
        """Genera y guarda el audio de todas las frases fijas del agente."""
        nuevas = 0
        for frase in frases:
            if self._clave(frase) not in self._cache and not self._leer_disco(frase):
                self._guardar(frase, self._sintetizar_completo(frase))
                nuevas += 1
        return nuevas

    def _leer_disco(self, texto: str) -> bytes | None:
        if not self._cache_dir:
            return None
        ruta = self._cache_dir / f"{self._clave(texto)}.pcm"
        if ruta.exists():
            datos = ruta.read_bytes()
            self._cache[self._clave(texto)] = datos
            return datos
        return None

    def _guardar(self, texto: str, audio: bytes) -> None:
        self._cache[self._clave(texto)] = audio
        if self._cache_dir:
            (self._cache_dir / f"{self._clave(texto)}.pcm").write_bytes(audio)

    def esta_cacheada(self, texto: str) -> bool:
        return self._clave(texto) in self._cache or bool(self._leer_disco(texto))

    # ---------------------------------------------------------------- sintesis

    def _sintetizar_completo(self, texto: str) -> bytes:
        # Piper no es seguro para uso concurrente; el lock evita corromper audio
        # cuando dos turnos coinciden.
        with self._lock:
            return b"".join(self._voz.synthesize_stream_raw(texto))

    def sintetizar(self, texto: str) -> Iterator[bytes]:
        """Emite PCM 16-bit mono. Sirve del cache si la frase es guionada."""
        texto = texto.strip()
        if not texto:
            return

        cacheado = self._cache.get(self._clave(texto)) or self._leer_disco(texto)
        if cacheado:
            yield cacheado
            return

        for oracion in oraciones(texto):
            yield self._sintetizar_completo(oracion)


def oraciones(texto: str) -> list[str]:
    """Trocea en oraciones para poder empezar a reproducir antes de terminar."""
    partes = [p.strip() for p in _FIN_ORACION.split(texto) if p and p.strip()]
    return partes or [texto.strip()]


def cabecera_wav(n_bytes: int, sample_rate: int) -> bytes:
    """Cabecera WAV mono 16-bit, para servir audio a un <audio> del navegador."""
    import struct

    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + n_bytes),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16),
            b"data",
            struct.pack("<I", n_bytes),
        ]
    )
