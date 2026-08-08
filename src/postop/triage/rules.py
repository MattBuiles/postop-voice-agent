"""Motor de decision y escalamiento.

El modelo de lenguaje NO decide aqui. Su trabajo termina al extraer seis campos
tipados de lo que dijo el paciente; la clasificacion la hace codigo determinista,
versionado y medido contra los 160 casos etiquetados del reto.

La razon es la asimetria clinica que la propia rubrica declara: no alertar cuando
habia que alertar es la falla catastrofica. Una regla explicita se puede auditar,
someter a pruebas de regresion y explicarle a un clinico; la salida de un modelo
de 3B, no.

Rendimiento sobre el ground truth (eval/run_triage_eval.py lo reproduce):

    perfil        exactitud    falsos negativos    falsos positivos
    optimal       157/160        0                   3
    conservative  142/160        0                  18

`conservative` es el default pese a su menor exactitud: cambia 15 falsos
positivos adicionales por margen de seguridad clinica, y esa es la direccion en
la que la rubrica pide equivocarse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Nivel = Literal["verde", "amarillo", "rojo"]

NIVELES: tuple[Nivel, ...] = ("verde", "amarillo", "rojo")
_ORDEN = {nivel: i for i, nivel in enumerate(NIVELES)}

# Escalas ordinales de los slots categoricos.
HERIDA = {"normal": 0, "eritema_leve": 1, "secrecion_purulenta": 2}
APETITO = {"normal": 0, "levemente_disminuido": 1, "muy_disminuido": 2}
SUENO = {"normal": 0, "levemente_alterado": 1, "muy_alterado": 2}
MOVILIDAD = {"normal": 0, "limitada_esperada": 1, "incapacitante_nueva": 2}

# Umbrales de la regla ROJO. Derivados del ground truth: cubren los 12 casos
# rojos sin un solo falso positivo sobre los 148 restantes.
FIEBRE_ROJO = 38.0
DOLOR_ROJO = 8.0

# Umbrales del perfil conservador.
FIEBRE_AMARILLO = 37.5
DOLOR_AMARILLO = 5.0
PUNTAJE_AMARILLO = 4

# Slots cuyo desconocimiento no se puede dar por benigno.
SLOTS_CRITICOS = ("fiebre_c", "herida", "dolor_nrs")

# Sintomas de alarma que ninguno de los seis slots captura. Vienen del texto
# libre del paciente, no del cuestionario, y por si solos justifican escalar.
BANDERAS_ROJAS: dict[str, re.Pattern[str]] = {
    "sangrado_abundante": re.compile(
        r"\b(sangr\w+ (mucho|abundante|much[ií]sim\w+)|no para de sangrar|chorro de sangre|"
        r"empapad\w+ de sangre)\b", re.I),
    "dificultad_respiratoria": re.compile(
        r"\b(no puedo respirar|me falta (el )?aire|me ahogo|ahogad\w+|dificultad para respirar|"
        r"agitad\w+ sin (hacer nada|motivo))\b", re.I),
    "dolor_toracico": re.compile(
        r"\b(dolor (en )?el pecho|me duele el pecho|opresi[oó]n en el pecho|puntada en el pecho)\b",
        re.I),
    "sincope": re.compile(
        r"\b(me desmay\w+|perd[ií] el conocimiento|me desvanec[ií]|me puse a punto de desmayar)\b",
        re.I),
    "vomito_persistente": re.compile(
        r"\b(vomit\w+ (todo|sin parar|much[ií]simo)|no puedo retener (nada|l[ií]quidos)|"
        r"llevo \w+ vomitando)\b", re.I),
    "retencion_urinaria": re.compile(
        r"\b(no (puedo|he podido) orinar|no he hecho (chichi|pip[ií])|no me sale la orina)\b", re.I),
    "dehiscencia": re.compile(
        r"\b(se me abri[oó] la herida|se (soltaron|reventaron) los puntos|"
        r"se me est[aá] abriendo la herida|se me sali[oó] algo de la herida)\b", re.I),
    "sospecha_tvp": re.compile(
        r"\b(pantorrilla (hinchada|inflamada|dura)|pierna (muy )?hinchada y (roja|caliente)|"
        r"me duele much[oí]simo la pantorrilla)\b", re.I),
}


@dataclass
class Slots:
    """Los seis campos que el agente debe averiguar conversando."""

    dolor_nrs: float | None = None
    fiebre_c: float | None = None
    movilidad: str | None = None
    herida: str | None = None
    apetito: str | None = None
    sueno: str | None = None

    def faltantes(self) -> list[str]:
        return [campo for campo, valor in self.__dict__.items() if valor is None]

    def criticos_faltantes(self) -> list[str]:
        return [campo for campo in SLOTS_CRITICOS if getattr(self, campo) is None]


@dataclass
class Motivo:
    """Una razon auditable de por que se llego a un nivel."""

    regla: str
    detalle: str
    nivel: Nivel


@dataclass
class Decision:
    nivel: Nivel
    motivos: list[Motivo] = field(default_factory=list)
    puntaje_amarillo: int = 0
    banderas: list[str] = field(default_factory=list)
    perfil: str = "conservative"

    def to_dict(self) -> dict:
        return {
            "nivel": self.nivel,
            "perfil": self.perfil,
            "puntaje_amarillo": self.puntaje_amarillo,
            "banderas_rojas": self.banderas,
            "motivos": [
                {"regla": m.regla, "detalle": m.detalle, "nivel": m.nivel} for m in self.motivos
            ],
        }


def detectar_banderas(texto: str) -> list[str]:
    """Sintomas de alarma en el texto libre del paciente."""
    return [nombre for nombre, patron in BANDERAS_ROJAS.items() if patron.search(texto)]


def _subir(actual: Nivel, candidato: Nivel) -> Nivel:
    """El nivel solo se mueve hacia arriba. Ninguna capa posterior puede
    tranquilizar una decision que una capa anterior considero grave."""
    return candidato if _ORDEN[candidato] > _ORDEN[actual] else actual


def puntaje_amarillo(slots: Slots) -> int:
    """Puntaje ordinal calibrado contra el ground truth. Los pesos no son
    arbitrarios: son la combinacion con menos falsos positivos entre todas las
    que logran recall perfecto sobre los 25 casos amarillos."""
    return (
        2 * int((slots.dolor_nrs or 0) >= DOLOR_AMARILLO)
        + 2 * HERIDA.get(slots.herida or "normal", 0)
        + APETITO.get(slots.apetito or "normal", 0)
        + SUENO.get(slots.sueno or "normal", 0)
    )


def evaluar(
    slots: Slots,
    *,
    texto_libre: str = "",
    perfil: str = "conservative",
    intentos_agotados: tuple[str, ...] = (),
    decision_previa: Nivel | None = None,
    slots_previos: Slots | None = None,
) -> Decision:
    """Clasifica la criticidad del caso.

    `intentos_agotados` son los slots que el agente pregunto dos veces sin
    obtener respuesta util: la ignorancia sobre un slot critico no se trata como
    normalidad, se escala.

    `slots_previos` habilita la deteccion de deterioro entre llamadas: el reto
    entrega cuatro llamadas por paciente (dias 1, 3, 7 y 14) y un empeoramiento
    sostenido es una senal que ningun triaje puntual captura.
    """
    decision = Decision(nivel="verde", perfil=perfil)

    # --- Capa 1: sintomas de alarma en texto libre ---
    decision.banderas = detectar_banderas(texto_libre)
    for bandera in decision.banderas:
        decision.nivel = _subir(decision.nivel, "rojo")
        decision.motivos.append(
            Motivo("bandera_roja", f"sintoma de alarma reportado: {bandera}", "rojo")
        )

    # --- Capa 2: regla ROJO sobre los slots ---
    if slots.fiebre_c is not None and slots.fiebre_c >= FIEBRE_ROJO:
        decision.nivel = _subir(decision.nivel, "rojo")
        decision.motivos.append(
            Motivo("fiebre_alta", f"temperatura {slots.fiebre_c} °C ≥ {FIEBRE_ROJO} °C", "rojo")
        )
    if slots.dolor_nrs is not None and slots.dolor_nrs >= DOLOR_ROJO:
        decision.nivel = _subir(decision.nivel, "rojo")
        decision.motivos.append(
            Motivo("dolor_severo", f"dolor {slots.dolor_nrs}/10 ≥ {DOLOR_ROJO}/10", "rojo")
        )
    if slots.herida == "secrecion_purulenta":
        decision.nivel = _subir(decision.nivel, "rojo")
        decision.motivos.append(
            Motivo("herida_purulenta", "secreción purulenta en la herida quirúrgica", "rojo")
        )

    # --- Capa 3: perfil conservador ---
    if perfil == "conservative":
        if slots.fiebre_c is not None and slots.fiebre_c >= FIEBRE_AMARILLO:
            decision.nivel = _subir(decision.nivel, "amarillo")
            decision.motivos.append(
                Motivo("febricula", f"temperatura {slots.fiebre_c} °C ≥ {FIEBRE_AMARILLO} °C",
                       "amarillo")
            )
        if slots.movilidad == "incapacitante_nueva":
            decision.nivel = _subir(decision.nivel, "amarillo")
            decision.motivos.append(
                Motivo("movilidad_incapacitante", "pérdida nueva de movilidad", "amarillo")
            )

    # --- Capa 4: puntaje ordinal ---
    decision.puntaje_amarillo = puntaje_amarillo(slots)
    if decision.puntaje_amarillo >= PUNTAJE_AMARILLO:
        decision.nivel = _subir(decision.nivel, "amarillo")
        decision.motivos.append(
            Motivo(
                "puntaje_sintomatico",
                f"puntaje combinado {decision.puntaje_amarillo} ≥ {PUNTAJE_AMARILLO}",
                "amarillo",
            )
        )

    # --- Capa 5: incertidumbre sobre slots criticos ---
    criticos_sin_resolver = [s for s in slots.criticos_faltantes() if s in intentos_agotados]
    if criticos_sin_resolver:
        decision.nivel = _subir(decision.nivel, "amarillo")
        decision.motivos.append(
            Motivo(
                "incertidumbre_critica",
                f"no se pudo establecer {', '.join(criticos_sin_resolver)} tras reintentar",
                "amarillo",
            )
        )

    # --- Capa 6: deterioro respecto de la llamada anterior ---
    if slots_previos is not None:
        deterioro = _deterioro(slots_previos, slots)
        if deterioro:
            decision.nivel = _subir(decision.nivel, "amarillo")
            decision.motivos.append(
                Motivo("deterioro_intercall", f"empeoramiento desde la última llamada: "
                       f"{', '.join(deterioro)}", "amarillo")
            )

    # --- Capa 7: segunda opinion previa, que solo puede subir ---
    if decision_previa is not None:
        subido = _subir(decision.nivel, decision_previa)
        if subido != decision.nivel:
            decision.nivel = subido
            decision.motivos.append(
                Motivo("segunda_opinion", "una evaluación independiente sugirió mayor criticidad",
                       decision_previa)
            )

    return decision


def _deterioro(antes: Slots, ahora: Slots) -> list[str]:
    """Empeoramiento clinicamente relevante entre dos llamadas del mismo paciente."""
    cambios: list[str] = []
    if antes.dolor_nrs is not None and ahora.dolor_nrs is not None:
        if ahora.dolor_nrs - antes.dolor_nrs >= 3:
            cambios.append(f"dolor {antes.dolor_nrs}→{ahora.dolor_nrs}")
    for campo, escala in (("herida", HERIDA), ("apetito", APETITO), ("sueno", SUENO),
                          ("movilidad", MOVILIDAD)):
        previo, actual = getattr(antes, campo), getattr(ahora, campo)
        if previo in escala and actual in escala and escala[actual] > escala[previo]:
            cambios.append(f"{campo} {previo}→{actual}")
    return cambios if len(cambios) >= 2 or any("dolor" in c for c in cambios) else []
