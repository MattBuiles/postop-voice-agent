"""Extraccion de temperatura y escala de dolor del habla coloquial colombiana.

Este modulo existe porque las dos variables que disparan el nivel ROJO del
triaje -- temperatura >= 38.0 y dolor >= 8 -- llegan habladas, y un modelo de 3B
es poco fiable convirtiendo "treinta y ocho y medio" en 38.5.

El riesgo concreto esta en los datos del reto. Un paciente etiquetado ROJO dice:

    "Me tomé la temperatura y salió como en 38, pero eso debe ser del calor de
     acá no más, doctora"

El valor medido es 38.0 y la interpretacion del paciente es "no es nada". Quedarse
con la interpretacion produce el falso negativo que la rubrica considera la falla
catastrofica. Este parser se queda con el numero.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

UNIDADES = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16, "diecisiete": 17,
    "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiuno": 21, "veintidos": 22,
    "veintitres": 23, "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26,
    "veintisiete": 27, "veintiocho": 28, "veintinueve": 29, "treinta": 30,
    "cuarenta": 40,
}

# Rangos fisiologicos plausibles. Fuera de ellos, el numero hablado casi siempre
# se refiere a otra cosa (la hora, los dias postoperatorios, la edad).
RANGO_TEMPERATURA = (34.0, 43.0)
RANGO_DOLOR = (0.0, 10.0)


@dataclass(frozen=True)
class Medicion:
    valor: float
    evidencia: str
    seguro: bool  # False cuando el valor se infirio de una expresion vaga


def _plano(texto: str) -> str:
    sin = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", sin)


def _palabras_a_numero(frase: str) -> float | None:
    """Convierte 'treinta y ocho y medio' o 'treinta y siete' en un numero."""
    partes = [p for p in frase.split() if p not in {"y", "de", "los", "el", "la"}]
    total = 0.0
    encontrado = False
    for i, palabra in enumerate(partes):
        if palabra in UNIDADES:
            total += UNIDADES[palabra]
            encontrado = True
        elif palabra in {"medio", "media"} and encontrado:
            total += 0.5
        elif palabra in {"punto", "con", "coma"} and i + 1 < len(partes) and partes[i + 1] in UNIDADES:
            # "treinta y seis con siete" = 36.7
            total += UNIDADES[partes[i + 1]] / 10.0
            break
    return total if encontrado else None


def extraer_temperatura(texto: str) -> Medicion | None:
    """Busca una temperatura corporal en el enunciado."""
    plano = _plano(texto)

    # "38.5", "38,5", "38 grados", "37 y medio", "37 y algo"
    for patron, ajuste in (
        (r"(\d{2})[.,](\d)\s*(?:grados?)?", None),
        (r"(\d{2})\s*(?:grados?|°)", None),
        (r"(\d{2})\s+y\s+(medio|media)", 0.5),
        # "37 y algo" / "38 y pico": hay decimales que el paciente no precisa.
        # Se asume el punto medio y se marca como no seguro.
        (r"(\d{2})\s+y\s+(algo|pico|tantos?)", 0.5),
    ):
        for coincidencia in re.finditer(patron, plano):
            if ajuste is None and coincidencia.lastindex == 2 and coincidencia.group(2).isdigit():
                valor = float(f"{coincidencia.group(1)}.{coincidencia.group(2)}")
                seguro = True
            elif ajuste is None:
                valor = float(coincidencia.group(1))
                seguro = True
            else:
                valor = float(coincidencia.group(1)) + ajuste
                seguro = coincidencia.group(2) in {"medio", "media"}
            if RANGO_TEMPERATURA[0] <= valor <= RANGO_TEMPERATURA[1]:
                return Medicion(valor, coincidencia.group(0).strip(), seguro)

    # Numero desnudo en rango fisiologico: "salió como en 38", "me marcó 37".
    # Va despues de los patrones explicitos para no ganarles, pero es
    # imprescindible: el paciente rara vez dice "grados". El caso ROJO del
    # dataset que mas riesgo tiene de perderse -- "salió como en 38, pero eso
    # debe ser del calor" -- solo lo captura esta rama.
    # Se excluyen los contextos donde un numero de dos digitos significa otra
    # cosa (edad, dias, horas), que es el unico falso positivo plausible.
    coincidencia = re.search(
        r"\b(\d{2})\b(?!\s*(?:anos|dias|horas|minutos|veces|kilos|km|%))", plano
    )
    if coincidencia:
        valor = float(coincidencia.group(1))
        if RANGO_TEMPERATURA[0] <= valor <= RANGO_TEMPERATURA[1]:
            return Medicion(valor, coincidencia.group(0).strip(), True)

    # Numeros escritos con palabras: "treinta y ocho y medio", "treinta y seis con siete"
    for coincidencia in re.finditer(
        r"(treinta|cuarenta)(?:\s+y\s+\w+)*(?:\s+(?:con|coma|punto)\s+\w+)?"
        r"(?:\s+y\s+(?:medio|media))?",
        plano,
    ):
        valor = _palabras_a_numero(coincidencia.group(0))
        if valor is not None and RANGO_TEMPERATURA[0] <= valor <= RANGO_TEMPERATURA[1]:
            return Medicion(valor, coincidencia.group(0).strip(), True)

    return None


def extraer_dolor(texto: str) -> Medicion | None:
    """Busca una intensidad de dolor en escala 0-10."""
    plano = _plano(texto)

    # Rangos: "un uno o dos", "entre 3 y 4", "como 5 o 6". Se toma el extremo
    # SUPERIOR por la asimetria clinica: subestimar el dolor es el error que
    # produce falsos negativos, y el falso negativo es la falla catastrofica.
    rango = re.search(
        r"\b(?:entre\s+)?(\d|cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s*"
        r"(?:o|a|y)\s*(\d|cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\b",
        plano,
    )
    if rango:
        valores = []
        for crudo in rango.groups():
            valores.append(float(UNIDADES[crudo]) if crudo in UNIDADES else float(crudo))
        alto = max(valores)
        if RANGO_DOLOR[0] <= alto <= RANGO_DOLOR[1]:
            return Medicion(alto, rango.group(0).strip(), True)

    # "un 6", "como un seis", "en 7", "6 de 10", "seis sobre diez"
    patrones = [
        r"\b(\d{1,2})\s*(?:/|\s+de\s+|\s+sobre\s+)\s*10\b",
        r"\b(?:como\s+)?(?:un|en|de)\s+(\d{1,2})\b",
        r"\bun\s+(cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\b",
        r"\b(?:como\s+)?(?:un\s+)?(cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s+"
        r"(?:de|sobre)\s+diez\b",
    ]
    for patron in patrones:
        coincidencia = re.search(patron, plano)
        if coincidencia:
            crudo = coincidencia.group(1)
            valor = float(UNIDADES[crudo]) if crudo in UNIDADES else float(crudo)
            if RANGO_DOLOR[0] <= valor <= RANGO_DOLOR[1]:
                return Medicion(valor, coincidencia.group(0).strip(), True)

    # Ultimo recurso: un numero suelto de un digito en un turno sobre dolor.
    coincidencia = re.search(r"\b(\d)\b", plano)
    if coincidencia:
        valor = float(coincidencia.group(1))
        if RANGO_DOLOR[0] <= valor <= RANGO_DOLOR[1]:
            return Medicion(valor, coincidencia.group(0).strip(), False)

    return None
