"""Transcripcion de voz a texto con faster-whisper.

El `initial_prompt` no es un adorno: sesga el decodificador hacia el vocabulario
clinico y los regionalismos colombianos del reto. Sin el, Whisper transcribe
"eritema" como "el tema" y "purulenta" como "pura lenta", y esos dos terminos
son justamente los que mueven el triaje.

El modelo `small` se elige por presupuesto de latencia: en CPU transcribe a
~0.25x tiempo real, asi que un turno de 4 segundos se resuelve en ~1 s. `medium`
mejora poco el espanol y duplica el tiempo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Por debajo de esta log-probabilidad media, la transcripcion no es de fiar.
#
# Medido en una llamada real: ante habla poco clara Whisper devolvio "Nada, no
# podios cancelir ni hormignada", y el modelo extrajo de ahi que el sueno era
# `normal`. Adivinar un slot a partir de ruido es peor que no tener el slot: el
# sistema ya sabe escalar por incertidumbre, pero no puede corregir un dato falso
# que da por bueno.
UMBRAL_CONFIANZA = -0.85

SESGO_DOMINIO = (
    "Llamada de seguimiento tras una cirugía. El paciente habla español colombiano y "
    "describe sus síntomas: dolor del cero al diez, fiebre, temperatura en grados, "
    "escalofríos, la herida quirúrgica, enrojecimiento, eritema, hinchazón, secreción, "
    "pus, purulenta, los puntos, la cicatriz, movilidad, caminar, apetito, sueño, "
    "apendicectomía, colecistectomía, colectomía, mastectomía, reemplazo de rodilla."
)


@dataclass
class Transcripcion:
    texto: str
    idioma: str
    ms: float
    probabilidad_media: float

    @property
    def fiable(self) -> bool:
        """False cuando el reconocedor estaba adivinando."""
        return self.probabilidad_media >= UMBRAL_CONFIANZA

    def to_dict(self) -> dict:
        return {
            "texto": self.texto,
            "ms": round(self.ms, 1),
            "confianza": round(self.probabilidad_media, 3),
            "fiable": self.fiable,
        }


class Transcriptor:
    def __init__(self, modelo: str = "small", *, cache_dir: Path | None = None) -> None:
        from faster_whisper import WhisperModel

        # int8 en CPU: ~4x mas rapido que float32 y sin perdida apreciable en
        # espanol para este tamano de modelo.
        self._modelo = WhisperModel(
            modelo,
            device="cpu",
            compute_type="int8",
            download_root=str(cache_dir) if cache_dir else None,
        )
        self.nombre = modelo

    def transcribir(self, audio: np.ndarray, *, sample_rate: int = 16000) -> Transcripcion:
        """`audio` es float32 mono normalizado a [-1, 1]."""
        inicio = time.perf_counter()
        segmentos, info = self._modelo.transcribe(
            audio,
            language="es",
            initial_prompt=SESGO_DOMINIO,
            beam_size=1,              # greedy: la calidad extra del beam no paga su latencia
            vad_filter=True,
            condition_on_previous_text=False,  # evita que un turno arrastre errores al siguiente
        )
        partes: list[str] = []
        probabilidades: list[float] = []
        for segmento in segmentos:
            partes.append(segmento.text)
            probabilidades.append(getattr(segmento, "avg_logprob", 0.0))

        texto = " ".join(p.strip() for p in partes).strip()
        if _es_eco_del_sesgo(texto):
            # Ante silencio o ruido, Whisper devuelve trozos de su propio
            # initial_prompt. Sin este filtro aparecia como si el paciente
            # hubiera dicho "El paciente habla del cero al diez, fiebre,".
            texto = ""

        return Transcripcion(
            texto=texto,
            idioma=info.language,
            ms=(time.perf_counter() - inicio) * 1000,
            probabilidad_media=float(np.mean(probabilidades)) if probabilidades else 0.0,
        )


_PALABRAS_SESGO = {
    p for p in "".join(c if c.isalnum() or c.isspace() else " " for c in SESGO_DOMINIO.lower()).split()
    if len(p) > 3
}


def _es_eco_del_sesgo(texto: str) -> bool:
    """¿La transcripcion es en realidad el prompt de sesgo devuelto por Whisper?

    Con audio en silencio o con solo ruido, Whisper tiende a "alucinar" el
    contenido de su initial_prompt. Se descarta cuando casi todas sus palabras
    salen de ese prompt y no aporta nada nuevo.
    """
    palabras = [
        p for p in "".join(c if c.isalnum() or c.isspace() else " " for c in texto.lower()).split()
        if len(p) > 3
    ]
    if len(palabras) < 3:
        return False
    del_sesgo = sum(1 for p in palabras if p in _PALABRAS_SESGO)
    return del_sesgo / len(palabras) >= 0.8


def pcm16_a_float32(datos: bytes) -> np.ndarray:
    """Convierte el PCM que llega del navegador al formato que espera Whisper."""
    return np.frombuffer(datos, dtype=np.int16).astype(np.float32) / 32768.0
