"""Backend de voz neuronal con las voces colombianas de Microsoft Edge.

Por que existe, si ya hay un backend local:

Piper sintetiza en 65-300 ms y no necesita red, pero suena sintetico y sus voces
en espanol son de Mexico, Espana o Argentina. Este backend usa
`es-CO-SalomeNeural` y `es-CO-GonzaloNeural`, que son **voces colombianas
nativas** y de calidad neuronal. Para un reto cuyos pacientes son colombianos,
eso se nota en cuanto el jurado lo escucha.

Cuesta ~665 ms al primer audio, diez veces mas que Piper. En la practica pesa
poco: casi todos los turnos del agente son texto fijo cuyo audio se pre-sintetiza
al arrancar y se sirve del cache, asi que ese coste solo lo pagan las respuestas
generadas, que son minoria.

**Lo que se pierde, y hay que decirlo:** deja de funcionar sin internet, y el
texto que dice el agente viaja a un servicio de Microsoft. El audio del paciente
NO sale de la maquina en ningun caso -- la transcripcion sigue siendo local --,
pero si el agente lee en voz alta algo derivado de lo que conto el paciente, ese
texto sale. Por eso el backend local sigue siendo el predeterminado: la promesa
de que nada del paciente abandona la maquina es mas valiosa que la naturalidad
de la voz, y quien prefiera lo contrario lo activa a proposito.

Tampoco es una API oficial ni contratada: puede dejar de funcionar sin aviso.
Para una sesion de evaluacion en vivo, eso es un riesgo real.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
from collections.abc import Iterator

# Frecuencia de salida. Se remuestrea al mismo valor que usa Piper para que el
# cliente no tenga que distinguir entre backends.
SAMPLE_RATE = 22050

VOCES_COLOMBIANAS = ("es-CO-SalomeNeural", "es-CO-GonzaloNeural")


class SintetizadorEdge:
    """Misma interfaz que `tts.voz.SintetizadorVoz`, para que sean intercambiables."""

    def __init__(self, voz: str = "es-CO-SalomeNeural", *, cache_dir=None) -> None:
        import edge_tts  # noqa: F401  (falla temprano si no esta instalado)

        if not shutil.which("ffmpeg"):
            raise RuntimeError(
                "el backend de voz 'edge' necesita ffmpeg para decodificar MP3 a PCM"
            )
        self.voz = voz
        self.sample_rate = SAMPLE_RATE
        self._cache: dict[str, bytes] = {}
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ cache

    @staticmethod
    def _clave(texto: str) -> str:
        import hashlib

        return hashlib.sha256(texto.strip().encode("utf-8")).hexdigest()[:20]

    def _leer_disco(self, texto: str) -> bytes | None:
        if not self._cache_dir:
            return None
        ruta = self._cache_dir / f"edge-{self._clave(texto)}.pcm"
        if ruta.exists():
            datos = ruta.read_bytes()
            self._cache[self._clave(texto)] = datos
            return datos
        return None

    def _guardar(self, texto: str, audio: bytes) -> None:
        self._cache[self._clave(texto)] = audio
        if self._cache_dir:
            (self._cache_dir / f"edge-{self._clave(texto)}.pcm").write_bytes(audio)

    def esta_cacheada(self, texto: str) -> bool:
        return self._clave(texto) in self._cache or bool(self._leer_disco(texto))

    def precalentar(self, frases: list[str]) -> int:
        """Sintetiza y guarda el texto fijo del agente. Aqui rinde el doble que
        en el backend local: ademas de ahorrar latencia, evita una llamada de red
        en mitad de la conversacion."""
        nuevas = 0
        for frase in frases:
            if not self.esta_cacheada(frase):
                self._guardar(frase, self._sintetizar(frase))
                nuevas += 1
        return nuevas

    # ---------------------------------------------------------------- sintesis

    def _sintetizar(self, texto: str) -> bytes:
        import edge_tts

        async def descargar() -> bytes:
            trozos: list[bytes] = []
            async for evento in edge_tts.Communicate(texto, self.voz).stream():
                if evento["type"] == "audio":
                    trozos.append(evento["data"])
            return b"".join(trozos)

        with self._lock:
            mp3 = asyncio.run(descargar())
        return _mp3_a_pcm(mp3)

    def sintetizar(self, texto: str) -> Iterator[bytes]:
        texto = texto.strip()
        if not texto:
            return
        cacheado = self._cache.get(self._clave(texto)) or self._leer_disco(texto)
        if cacheado:
            yield cacheado
            return
        yield self._sintetizar(texto)


def _mp3_a_pcm(mp3: bytes) -> bytes:
    """MP3 -> PCM 16 bits mono, al mismo sample rate que el backend local."""
    proceso = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"],
        input=mp3, capture_output=True, check=False,
    )
    if proceso.returncode != 0:
        raise RuntimeError(f"ffmpeg falló al decodificar el audio: {proceso.stderr[:200]!r}")
    return proceso.stdout
