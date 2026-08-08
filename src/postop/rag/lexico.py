"""Puente entre el habla del paciente y el vocabulario de la literatura clinica.

El corpus dice "purulent discharge", "dehiscencia", "ileo postoperatorio". El
paciente dice "me sale pus", "se me abrio", "no he podido hacer del cuerpo". Un
modelo denso cubre parte de esa distancia, pero el benchmark de embed.py mostro
que falla 4 de cada 10 consultas coloquiales.

Este diccionario es deterministico a proposito: no cuesta latencia, no alucina y
se puede auditar linea por linea, a diferencia de una expansion de consulta
generada por el modelo.
"""

from __future__ import annotations

import re
import unicodedata

# coloquial -> terminos clinicos que deben entrar a la busqueda lexica
SINONIMOS: dict[str, tuple[str, ...]] = {
    # herida
    "pus": ("purulenta", "purulent", "supuracion", "infeccion sitio operatorio"),
    "materia": ("purulenta", "purulent", "supuracion"),
    "rojito": ("eritema", "erythema", "celulitis"),
    "rojita": ("eritema", "erythema", "celulitis"),
    "colorada": ("eritema", "erythema"),
    "hinchado": ("edema", "swelling", "inflamacion"),
    "hinchada": ("edema", "swelling", "inflamacion"),
    "se abrio": ("dehiscencia", "dehiscence", "evisceracion"),
    "los puntos": ("sutura", "suture", "grapas"),
    "costra": ("cicatrizacion", "wound healing"),
    "liquido": ("secrecion", "drainage", "seroma"),
    "bulto": ("seroma", "hematoma", "coleccion"),
    # fiebre
    "calentura": ("fiebre", "fever", "hipertermia"),
    "calorcito": ("febricula", "low grade fever"),
    "escalofrio": ("fiebre", "chills", "escalofrios"),
    "destemplado": ("fiebre", "febricula"),
    # dolor
    "punzada": ("dolor agudo", "sharp pain"),
    "ardor": ("dolor urente", "burning pain"),
    "colico": ("dolor colico", "cramping"),
    "aporriado": ("dolor", "malestar general"),
    # digestivo
    "hacer del cuerpo": ("deposicion", "transito intestinal", "ileo postoperatorio", "bowel movement"),
    "del bano": ("deposicion", "transito intestinal"),
    "gases": ("flatos", "transito intestinal", "ileo"),
    "vomito": ("emesis", "vomiting", "nausea"),
    "devolver": ("emesis", "vomiting"),
    "sin ganas de comer": ("hiporexia", "anorexia", "appetite"),
    # respiratorio / circulatorio
    "me falta el aire": ("disnea", "dyspnea", "tromboembolismo pulmonar"),
    "ahogo": ("disnea", "dyspnea"),
    "pantorrilla": ("trombosis venosa profunda", "deep vein thrombosis", "pantorrilla"),
    # urinario
    "orinar": ("retencion urinaria", "urinary retention", "miccion"),
    "hacer chichi": ("retencion urinaria", "miccion"),
    # movilidad
    "caminar": ("deambulacion", "ambulation", "movilizacion"),
    "levantarme": ("deambulacion", "movilizacion precoz"),
    # sueno
    "pegar el ojo": ("insomnio", "alteracion del sueno", "sleep"),
    "dormir": ("sueno", "insomnio", "sleep"),
}

_PALABRAS_VACIAS = {
    "el", "la", "los", "las", "un", "una", "de", "del", "y", "o", "que", "en", "a", "con", "por",
    "para", "me", "mi", "se", "le", "lo", "es", "esta", "muy", "como", "no", "si", "ya", "pero",
    "usted", "yo", "he", "ha", "hace", "desde", "mas", "algo", "eso", "esto", "ahi", "aqui",
}


def sin_tildes(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


def expandir(consulta: str) -> list[str]:
    """Terminos clinicos anadidos por el diccionario, sin repetir."""
    plano = sin_tildes(consulta)
    anadidos: list[str] = []
    for coloquial, clinicos in SINONIMOS.items():
        if sin_tildes(coloquial) in plano:
            anadidos.extend(t for t in clinicos if t not in anadidos)
    return anadidos


def consulta_fts(texto: str) -> str:
    """Traduce lenguaje natural a una expresion MATCH de FTS5.

    Se citan todos los terminos porque el texto del paciente puede traer
    comillas, asteriscos o guiones que FTS5 interpretaria como operadores y que
    harian fallar la consulta entera con un error de sintaxis.
    """
    plano = sin_tildes(texto)
    terminos = [t for t in re.findall(r"[a-z0-9]{3,}", plano) if t not in _PALABRAS_VACIAS]
    for expansion in expandir(texto):
        terminos.extend(t for t in sin_tildes(expansion).split() if len(t) >= 3)

    vistos: list[str] = []
    for termino in terminos:
        if termino not in vistos:
            vistos.append(termino)
    return " OR ".join(f'"{t}"' for t in vistos[:40])
