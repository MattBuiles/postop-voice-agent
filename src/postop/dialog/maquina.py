"""Maquina de estados de la conversacion.

El agente no improvisa el rumbo de la llamada: recorre seis slots en orden fijo,
igual que el protocolo que aparece en las 160 conversaciones del dataset del
reto (dolor, fiebre, movilidad, herida, apetito, sueno). Eso permite tres cosas
que un bucle libre de LLM no da:

  - **Latencia**: las preguntas canonicas son texto fijo, asi que su audio se
    pre-sintetiza y suena sin esperar al modelo.
  - **Cobertura**: ningun slot se queda sin preguntar porque el modelo se
    distrajo.
  - **Auditabilidad**: en cualquier momento se puede decir en que punto del
    protocolo esta la llamada y por que.

Sobre ese esqueleto se apilan los desvios: si el paciente pregunta algo, se le
responde con RAG anclado y se vuelve exactamente al slot pendiente.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from postop.dialog import guardas
from postop.llm.extract import Extraccion
from postop.triage.rules import Decision, Slots, detectar_banderas, evaluar

ORDEN_SLOTS: tuple[str, ...] = (
    "dolor_nrs",
    "fiebre_c",
    "movilidad",
    "herida",
    "apetito",
    "sueno",
)

MAX_INTENTOS_POR_SLOT = 2

# Preguntas canonicas. Texto fijo -> audio pre-sintetizado -> 0 ms de TTS en la
# mayoria de los turnos de la llamada, que es lo que sostiene el P50 reportado.
PREGUNTAS: dict[str, str] = {
    "dolor_nrs": "¿Cómo ha sentido el dolor desde la cirugía? En una escala del cero al diez, "
                 "¿qué tanto le duele?",
    "fiebre_c": "¿Ha sentido escalofríos o se ha tomado la temperatura estos días?",
    "movilidad": "¿Cómo ha estado para moverse, para caminar o levantarse de la cama?",
    "herida": "¿Cómo ha visto la herida? ¿Le nota enrojecimiento, hinchazón o alguna secreción?",
    "apetito": "¿Cómo ha estado su apetito? ¿Ha podido comer con normalidad?",
    "sueno": "¿Cómo ha dormido estas noches? ¿Ha logrado descansar?",
}

REPREGUNTAS: dict[str, str] = {
    "dolor_nrs": "Para poder anotarlo bien: si cero es nada de dolor y diez es el peor dolor, "
                 "¿en qué número lo pondría?",
    "fiebre_c": "¿Alguien le tomó la temperatura con termómetro? Si se acuerda del número, "
                "me sirve mucho.",
    "movilidad": "¿Ha logrado levantarse de la cama y caminar por la casa, o no ha podido?",
    "herida": "¿La herida se ve del color normal de la piel, o la nota roja, o le sale algún "
              "líquido?",
    "apetito": "¿Ha estado comiendo como siempre, o menos de lo normal?",
    "sueno": "¿Logra dormir de corrido, se despierta a ratos, o casi no duerme?",
}

SALUDO = (
    "Buenos días, le habla el asistente automático de seguimiento de su cirugía. "
    "No soy un profesional de salud, pero le voy a hacer unas preguntas cortas sobre "
    "cómo va su recuperación y le paso el reporte al equipo médico. ¿Le parece bien?"
)

# Los cierres son condicionales al triaje. El de verde enumera senales de alarma
# a proposito: la rubrica penaliza explicitamente "tranquilizar al paciente ante
# un sintoma de alarma", asi que el agente nunca dice que todo esta bien y ya.
CIERRES: dict[str, str] = {
    "verde": "Por lo que me cuenta, su recuperación va como se espera. De todas formas, "
             "si le aparece fiebre de treinta y ocho grados o más, si la herida le empieza a "
             "supurar, o si el dolor aumenta fuerte, comuníquese de inmediato con su EPS. "
             "Gracias por su tiempo.",
    "amarillo": "Le agradezco la información. Hay un par de cosas que quiero que revise el "
                "equipo médico, así que voy a dejar reportado su caso para que lo contacten "
                "en las próximas horas. Si algo empeora antes, no espere y consulte.",
    "rojo": "Por lo que me está contando, esto necesita valoración médica ahora, no después. "
            "Voy a reportar su caso de inmediato al equipo. Por favor no espere: diríjase al "
            "servicio de urgencias o llame a su EPS ya mismo.",
}

INTERRUPCION_ROJO = (
    "Espere un momento, eso que me cuenta es importante y no quiero dejarlo pasar."
)

# Backchannels por tramo de silencio. Diseñar el silencio es un criterio
# explicito de la rubrica ("qué hace tu solución durante los silencios").
SILENCIOS: tuple[tuple[float, str], ...] = (
    (3.0, "Aquí sigo, tómese su tiempo."),
    (8.0, "¿Me escucha bien?"),
    (15.0, "Si prefiere, lo llamamos en otro momento. ¿Sigue ahí?"),
)

_PATRON_TERCERO = re.compile(
    r"\b(soy (el|la) (cuidador|hija|hijo|esposa|esposo|señora|mamá|papá)|"
    r"habla (la|el) (esposa|esposo|hija|hijo)|él no (escucha|puede)|"
    r"ella no (escucha|puede)|yo le cuento|le ayudo a responder)\b",
    re.I,
)

Fase = Literal["saludo", "protocolo", "cierre", "terminada"]


@dataclass
class Turno:
    hablante: str
    texto: str
    slot: str | None = None
    extraccion: dict | None = None
    decision: dict | None = None
    citas: list[dict] = field(default_factory=list)
    latencias: dict = field(default_factory=dict)
    tokens: dict = field(default_factory=dict)


@dataclass
class AccionAgente:
    """Lo que el agente debe decir y por que."""

    texto: str
    fase: Fase
    slot_actual: str | None
    es_guionada: bool          # True -> el audio puede venir del cache pre-sintetizado
    decision: Decision | None = None
    citas: list[dict] = field(default_factory=list)
    cerrar_llamada: bool = False
    nota: str = ""

    def to_dict(self) -> dict:
        return {
            "texto": self.texto,
            "fase": self.fase,
            "slot_actual": self.slot_actual,
            "es_guionada": self.es_guionada,
            "decision": self.decision.to_dict() if self.decision else None,
            "citas": self.citas,
            "cerrar_llamada": self.cerrar_llamada,
            "nota": self.nota,
        }


@dataclass
class EstadoLlamada:
    call_id: str
    paciente_id: str | None = None
    procedimiento: str | None = None
    escenario: str | None = None
    dia_postop: int | None = None
    perfil_triaje: str = "conservative"

    slots: Slots = field(default_factory=Slots)
    slots_previos: Slots | None = None
    fase: Fase = "saludo"
    indice_slot: int = 0
    intentos: dict[str, int] = field(default_factory=dict)
    agotados: list[str] = field(default_factory=list)
    turnos: list[Turno] = field(default_factory=list)
    texto_libre: list[str] = field(default_factory=list)
    banderas: list[str] = field(default_factory=list)
    participo_tercero: bool = False
    decision: Decision | None = None

    @property
    def slot_actual(self) -> str | None:
        if self.indice_slot < len(ORDEN_SLOTS):
            return ORDEN_SLOTS[self.indice_slot]
        return None

    def registrar(self, turno: Turno) -> None:
        self.turnos.append(turno)

    def evaluar(self) -> Decision:
        self.decision = evaluar(
            self.slots,
            texto_libre=" ".join(self.texto_libre),
            perfil=self.perfil_triaje,
            intentos_agotados=tuple(self.agotados),
            slots_previos=self.slots_previos,
        )
        return self.decision


def abrir(estado: EstadoLlamada) -> AccionAgente:
    """Primer turno del agente: identificarse antes de preguntar nada.

    Presentarse como sistema automatico no lo pide la rubrica de forma explicita,
    pero hacerse pasar por humano en un contexto clinico seria una falla grave.
    """
    return AccionAgente(SALUDO, "saludo", None, es_guionada=True, nota="apertura")


def es_tercero(texto: str) -> bool:
    return bool(_PATRON_TERCERO.search(texto))


def siguiente_pregunta(estado: EstadoLlamada) -> AccionAgente:
    """Pregunta del slot pendiente, o cierre si ya no quedan."""
    slot = estado.slot_actual
    if slot is None:
        return cerrar(estado)
    intentos = estado.intentos.get(slot, 0)
    texto = PREGUNTAS[slot] if intentos == 0 else REPREGUNTAS[slot]
    return AccionAgente(texto, "protocolo", slot, es_guionada=True)


def cerrar(estado: EstadoLlamada) -> AccionAgente:
    decision = estado.evaluar()
    return AccionAgente(
        CIERRES[decision.nivel],
        "cierre",
        None,
        es_guionada=True,
        decision=decision,
        cerrar_llamada=True,
        nota=f"triaje {decision.nivel}",
    )


def avanzar(estado: EstadoLlamada, extraccion: Extraccion) -> None:
    """Integra lo extraido y decide si el slot se da por resuelto."""
    slot = estado.slot_actual
    if slot is None:
        return

    for campo, valor in extraccion.slots.__dict__.items():
        if valor is not None:
            setattr(estado.slots, campo, valor)

    if getattr(estado.slots, slot) is not None:
        estado.indice_slot += 1
        return

    intentos = estado.intentos.get(slot, 0) + 1
    estado.intentos[slot] = intentos
    if intentos >= MAX_INTENTOS_POR_SLOT:
        # No se insiste una tercera vez: se marca como agotado y el motor de
        # triaje escala por incertidumbre en vez de asumir normalidad.
        estado.agotados.append(slot)
        estado.indice_slot += 1


def revisar_urgencia(estado: EstadoLlamada, texto: str) -> Decision | None:
    """Corta el protocolo si aparece algo que no puede esperar al final."""
    estado.texto_libre.append(texto)
    nuevas = [b for b in detectar_banderas(texto) if b not in estado.banderas]
    estado.banderas.extend(nuevas)

    decision = estado.evaluar()
    if decision.nivel == "rojo":
        return decision
    return None


def guarda_entrada(texto: str) -> guardas.Veredicto:
    return guardas.revisar_entrada(texto)


def backchannel(segundos_de_silencio: float) -> str | None:
    """Que decir tras un silencio, segun cuanto lleve."""
    elegido = None
    for umbral, frase in SILENCIOS:
        if segundos_de_silencio >= umbral:
            elegido = frase
    return elegido


def frases_pre_sintetizables() -> list[str]:
    """Todo el texto fijo del agente, para generar su audio al arrancar."""
    frases = [SALUDO, INTERRUPCION_ROJO, guardas.RESPUESTA_INYECCION,
              guardas.RESPUESTA_PRESCRIPCION,
              "Perdón, no le escuché bien. ¿Me lo repite, por favor?"]
    frases.extend(PREGUNTAS.values())
    frases.extend(REPREGUNTAS.values())
    frases.extend(CIERRES.values())
    frases.extend(frase for _, frase in SILENCIOS)
    return frases
