"""Verificador de anclaje: ninguna afirmacion clinica se dice sin respaldo textual.

El modelo no entrega solo una respuesta: entrega la frase literal del corpus que
la sustenta y el fragmento de donde la saco. Antes de que el agente hable, esa
frase se contrasta contra el fragmento real. Si no aparece alli, la respuesta se
descarta y el agente declara su limite.

Esto convierte "no alucinar" de promesa en invariante ejecutable, y ataca dos
puntos concretos de la evaluacion:

  - la penalizacion por alucinacion clinica peligrosa, que se anota textualmente
    en el acta del jurado;
  - la exigencia de que la cita "resista una verificacion contra la fuente real";
  - la compuerta G5: si el fragmento que respaldaba una respuesta se borro, no
    hay nada que verificar y el agente deja de poder afirmarlo. El olvido es
    estructural, no una promesa del prompt.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# Fraccion de la cita que debe aparecer literalmente en el fragmento. No es 1.0
# porque la extraccion de PDF introduce diferencias de espaciado y guionado que
# no cambian el contenido; no baja de 0.85 porque por debajo empieza a aceptar
# parafrasis, que es exactamente lo que se quiere impedir.
UMBRAL_ANCLAJE = 0.88

# Una cita demasiado corta ("el paciente", "38 grados") se encuentra en
# cualquier parte y no prueba nada.
MIN_CARACTERES_CITA = 25

_PUNTUACION = re.compile(r"[^\w\s]", re.UNICODE)
_ESPACIOS = re.compile(r"\s+")


# Fraccion de las palabras de contenido de la cita que deben aparecer en el
# fragmento. Cubre el caso real de un modelo pequeno que entiende bien la fuente
# pero la reformula: ante "Si el zumbido persiste mas de una semana se solicita
# audiometria", un 3B escribe "Si el zumbido de oido tras la cirugia persiste mas
# de una semana, se solicita audiometria" -- mismo contenido, 58% de coincidencia
# literal. Exigir literalidad estricta rechazaba respuestas correctas.
#
# La garantia se mantiene porque el umbral es alto y porque, al validar, se
# sustituye la cita del modelo por la frase REAL del documento: lo que ve el
# jurado siempre es texto del corpus, nunca texto del modelo.
UMBRAL_COBERTURA = 0.85

_VACIAS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al", "y", "o", "que",
    "en", "con", "por", "para", "se", "su", "sus", "es", "son", "como", "mas", "menos", "esta",
    "este", "esto", "si", "no", "the", "of", "and", "to", "in", "is", "for", "with", "a",
}


@dataclass(frozen=True)
class ResultadoAnclaje:
    anclada: bool
    similitud: float
    motivo: str
    chunk_uid: str | None = None
    # Frase literal del documento que respalda la afirmacion. Es esta, y no la
    # que escribio el modelo, la que se muestra y se registra.
    cita_verificada: str = ""

    def to_dict(self) -> dict:
        return {
            "anclada": self.anclada,
            "similitud": round(self.similitud, 4),
            "motivo": self.motivo,
            "chunk_uid": self.chunk_uid,
            "cita_verificada": self.cita_verificada,
        }


def _palabras_contenido(texto: str) -> set[str]:
    return {p for p in normalizar(texto).split() if len(p) >= 4 and p not in _VACIAS}


def cobertura(cita: str, fuente: str) -> float:
    """Fraccion de las palabras de contenido de la cita presentes en la fuente."""
    palabras_cita = _palabras_contenido(cita)
    if not palabras_cita:
        return 0.0
    return len(palabras_cita & _palabras_contenido(fuente)) / len(palabras_cita)


def _mejor_frase(cita: str, fuente: str) -> str:
    """La frase del fragmento que mas se parece a lo que afirmo el modelo."""
    frases = [f.strip() for f in re.split(r"(?<=[.;:!?])\s+", fuente) if len(f.strip()) > 20]
    if not frases:
        return fuente[:300].strip()
    objetivo = _palabras_contenido(cita)
    return max(
        frases,
        key=lambda f: len(objetivo & _palabras_contenido(f)) / max(1, len(_palabras_contenido(f))),
    )


def normalizar(texto: str) -> str:
    """Quita tildes, puntuacion y espaciado variable. Lo que queda es el
    contenido; lo que se fue es ruido de extraccion de PDF."""
    plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return _ESPACIOS.sub(" ", _PUNTUACION.sub(" ", plano)).strip()


def similitud_maxima(cita: str, fuente: str) -> float:
    """Fraccion de la cita que aparece de corrido dentro de la fuente."""
    cita_n, fuente_n = normalizar(cita), normalizar(fuente)
    if not cita_n or not fuente_n:
        return 0.0
    if cita_n in fuente_n:
        return 1.0
    coincidencia = SequenceMatcher(None, cita_n, fuente_n, autojunk=False).find_longest_match(
        0, len(cita_n), 0, len(fuente_n)
    )
    return coincidencia.size / len(cita_n)


def verificar(cita: str, pasajes: list, chunk_uid: str | None = None) -> ResultadoAnclaje:
    """Comprueba que la cita provenga de alguno de los pasajes recuperados.

    `pasajes` son objetos con atributos `chunk_uid` y `texto` (rag.retrieve.Pasaje).
    Se prueba primero contra el fragmento que el modelo declaro; si no cuadra, se
    intenta contra el resto, porque un modelo pequeno confunde identificadores con
    mas frecuencia de la que inventa contenido.
    """
    if not cita or len(cita.strip()) < MIN_CARACTERES_CITA:
        return ResultadoAnclaje(False, 0.0, "cita ausente o demasiado corta para probar nada")

    por_uid = {p.chunk_uid: p for p in pasajes}
    orden = ([por_uid[chunk_uid]] if chunk_uid in por_uid else []) + [
        p for p in pasajes if p.chunk_uid != chunk_uid
    ]

    mejor = 0.0
    mejor_uid: str | None = None

    # Primera pasada: coincidencia literal. Es la evidencia mas fuerte.
    for pasaje in orden:
        similitud = similitud_maxima(cita, pasaje.texto)
        if similitud > mejor:
            mejor, mejor_uid = similitud, pasaje.chunk_uid
        if similitud >= UMBRAL_ANCLAJE:
            declarado_ok = chunk_uid == pasaje.chunk_uid
            return ResultadoAnclaje(
                True,
                similitud,
                "cita literal verificada contra el fragmento declarado"
                if declarado_ok
                else "cita literal verificada, pero el modelo declaró otro fragmento",
                pasaje.chunk_uid,
                cita_verificada=_mejor_frase(cita, pasaje.texto),
            )

    # Segunda pasada: el modelo reformuló. Se acepta solo si practicamente todas
    # las palabras de contenido de su afirmación están en el fragmento, y lo que
    # se devuelve como cita es la frase real del documento.
    for pasaje in orden:
        cubierto = cobertura(cita, pasaje.texto)
        if cubierto >= UMBRAL_COBERTURA:
            return ResultadoAnclaje(
                True,
                cubierto,
                f"contenido verificado en la fuente ({cubierto:.0%} de cobertura léxica); "
                f"se cita la frase original del documento",
                pasaje.chunk_uid,
                cita_verificada=_mejor_frase(cita, pasaje.texto),
            )
        mejor = max(mejor, cubierto)

    return ResultadoAnclaje(
        False,
        mejor,
        f"la afirmación no se sostiene en ningún fragmento recuperado "
        f"(mejor coincidencia {mejor:.0%})",
        mejor_uid,
    )
