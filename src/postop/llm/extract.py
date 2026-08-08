"""Extraccion de los seis slots clinicos a partir de lo que dijo el paciente.

Division de trabajo, decidida por medicion y no por gusto:

  - **Temperatura y dolor**: parser deterministico (`asr.numeros`). 108/117
    aciertos sobre los enunciados reales del dataset y, lo que importa, cero
    errores en la direccion peligrosa. Un modelo de 3B no compite con eso.
  - **Los cuatro slots categoricos**: el modelo, con salida estructurada y
    **una sola pregunta por invocacion**.
  - **Evidencia y deteccion de preguntas**: codigo. El enunciado del paciente ya
    esta almacenado literal, asi que pedirle al modelo que lo copie solo gasta
    tokens.

Por que una invocacion por slot en vez de extraer los cuatro de una vez:

    variante                        aciertos   P50
    los 4 slots + evidencia         14/24      7255 ms
    un slot, enum acotado           ver README ~1100 ms

Pedir los cuatro obligaba al modelo a emitir ~50 tokens por turno, y en CPU cada
token cuesta ~80 ms. Acotar la salida a un enum de tres valores deja la
invocacion en un puñado de tokens, que es lo que hace viable la conversacion de
voz sobre un modelo pequeno.

La regla que gobierna el prompt: **se extrae lo medido, no lo que el paciente
opina de lo medido**. En los datos del reto hay un caso ROJO donde el paciente
dice "salió como en 38, pero eso debe ser del calor de acá no más". El valor es
38.0; la tranquilizacion es ruido.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from postop.asr import numeros
from postop.llm.client import ClienteLLM, Respuesta
from postop.triage.rules import Slots


@dataclass(frozen=True)
class EspecSlot:
    pregunta: str
    valores: tuple[str, ...]
    criterio: str
    ejemplos: tuple[tuple[str, str], ...]


# Un criterio por slot, redactado para empujar hacia "normal" salvo evidencia
# explicita: la version generica sobrecalificaba la gravedad (decia
# incapacitante_nueva ante "camino tranquila, sin problema").
SLOTS: dict[str, EspecSlot] = {
    "movilidad": EspecSlot(
        pregunta="la movilidad: caminar, levantarse de la cama",
        valores=("normal", "limitada_esperada", "incapacitante_nueva"),
        criterio=(
            "normal = se mueve y camina sin problema, aunque use andador o vaya con cuidado.\n"
            "limitada_esperada = va despacio o le cuesta, pero SE MUEVE por sí mismo.\n"
            "incapacitante_nueva = NO puede levantarse o NO puede caminar. "
            "Solo si el paciente dice que no puede, no si dice que le cuesta."
        ),
        ejemplos=(
            ("Me muevo bien, camino normal con el andador que me dieron.", "normal"),
            ("Me muevo despacito, como es de esperarse. Camino pasos cortos.",
             "limitada_esperada"),
            ("No he podido levantarme de la cama, no logro pararme.", "incapacitante_nueva"),
        ),
    ),
    "herida": EspecSlot(
        pregunta="la herida quirúrgica",
        valores=("normal", "eritema_leve", "secrecion_purulenta"),
        criterio=(
            "normal = sin enrojecimiento, sin secreción, sin nada raro.\n"
            "eritema_leve = enrojecimiento o hinchazón, SIN pus.\n"
            "secrecion_purulenta = pus, materia, líquido amarillo o verde, o mal olor."
        ),
        ejemplos=(
            ("La he visto normalita, sin nada raro, ni rojo ni hinchada.", "normal"),
            ("Se ve un poquito rojita ahí en el borde, pero nada de pus.", "eritema_leve"),
            ("Le sale como un líquido amarillo y huele feo.", "secrecion_purulenta"),
        ),
    ),
    "apetito": EspecSlot(
        pregunta="el apetito",
        valores=("normal", "levemente_disminuido", "muy_disminuido"),
        criterio=(
            "normal = come normal o con ganas, aunque comente dudas sobre la comida.\n"
            "levemente_disminuido = come menos de lo habitual pero come.\n"
            "muy_disminuido = casi no come, no le pasa la comida, no le da hambre."
        ),
        ejemplos=(
            ("He comido normal, con ganas y todo.", "normal"),
            ("Como poquito, se me han quitado un poco las ganas.", "levemente_disminuido"),
            ("Casi no me da hambre, como poquitico, todo se me revuelve.", "muy_disminuido"),
        ),
    ),
    "sueno": EspecSlot(
        pregunta="el sueño",
        valores=("normal", "levemente_alterado", "muy_alterado"),
        criterio=(
            "normal = duerme bien, aunque mencione alguna incomodidad menor.\n"
            "levemente_alterado = se despierta algunas veces pero descansa.\n"
            "muy_alterado = casi no duerme, no pega el ojo, pasa la noche en vela."
        ),
        ejemplos=(
            ("He dormido bien, apenas un poco de incomodidad al acostarme.", "normal"),
            ("Me despierto varias veces en la noche pero vuelvo a dormirme.",
             "levemente_alterado"),
            ("Casi no pego el ojo, paso la noche dando vueltas.", "muy_alterado"),
        ),
    ),
}

SLOT_A_PREGUNTA = {
    "dolor_nrs": "el dolor",
    "fiebre_c": "la fiebre o la temperatura",
    **{k: v.pregunta for k, v in SLOTS.items()},
}

# Deteccion de preguntas sin modelo: gratis, instantanea y mas fiable que un 3B.
_PATRON_PREGUNTA = re.compile(
    r"¿[^?]{4,}\?|\b(será que|usted (cree|sabe)|eso es (normal|malo|grave)|"
    r"qué (hago|significa)|puedo|debo|cuándo|cuánto|por qué)\b",
    re.I,
)

# Senales de que el paciente esquivo la pregunta o cambio de tema.
_PATRON_EVASION = re.compile(
    r"\b(mejor (hablemos|cuénteme)|cambiando de tema|no sé|ni idea|no me acuerdo|"
    r"prefiero no)\b",
    re.I,
)

# Piso deterministico para el estado de la herida.
#
# El modelo subestimaba: ante "se ve un poquito rojita alrededor, pero nada del
# otro mundo" respondia `normal`. Eritema leve aporta 2 de los 4 puntos que
# separan verde de amarillo, asi que perderlo mueve el triaje en la unica
# direccion que la rubrica considera catastrofica.
#
# Estas expresiones solo pueden SUBIR la severidad que dijo el modelo, nunca
# bajarla: misma asimetria que gobierna el resto del sistema.
_PISO_HERIDA: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(pus|materia|purulent\w*|amarillent\w*|mal olor|huele (feo|mal)|"
                r"l[ií]quido (amarillo|verde))\b", re.I), "secrecion_purulenta"),
    (re.compile(r"\b(roj\w+|colorad\w+|enrojec\w+|irritad\w+|hinchad\w+|inflamad\w+|"
                r"eritema)\b", re.I), "eritema_leve"),
)

# Negaciones que invalidan el piso: "ni rojo ni hinchada", "nada de pus".
_NEGACION_HERIDA = re.compile(
    r"\b(ni|sin|nada de|no (le )?(sale|tiene|hay|est[aá])|no se ve)\s+\w{0,12}\s*"
    r"(roj\w+|colorad\w+|hinchad\w+|inflamad\w+|pus|materia|secreci[oó]n)",
    re.I,
)


def _piso_herida(texto: str) -> str | None:
    """Severidad minima de la herida deducible del texto, o None."""
    for patron, valor in _PISO_HERIDA:
        for coincidencia in patron.finditer(texto):
            # Se descarta la mencion si aparece dentro de una negacion cercana.
            ventana = texto[max(0, coincidencia.start() - 40) : coincidencia.end()]
            if not _NEGACION_HERIDA.search(ventana):
                return valor
    return None


@dataclass
class Extraccion:
    slots: Slots
    evidencia: str = ""
    respondio: bool = False
    pregunta_del_paciente: str | None = None
    fuentes: dict[str, str] = field(default_factory=dict)  # slot -> parser | modelo
    respuesta_llm: Respuesta | None = None

    def to_dict(self) -> dict:
        return {
            "slots": {k: v for k, v in self.slots.__dict__.items() if v is not None},
            "evidencia": self.evidencia,
            "respondio": self.respondio,
            "pregunta_del_paciente": self.pregunta_del_paciente,
            "fuentes": self.fuentes,
        }


def detectar_pregunta(texto: str) -> str | None:
    """Devuelve la pregunta del paciente, si la hizo."""
    coincidencia = re.search(r"¿[^?]{4,}\?", texto)
    if coincidencia:
        return coincidencia.group(0)
    return texto.strip() if _PATRON_PREGUNTA.search(texto) else None


def _esquema(espec: EspecSlot) -> dict:
    return {
        "type": "object",
        "properties": {
            "valor": {"type": ["string", "null"], "enum": [*espec.valores, None]},
        },
        # `valor` va en required aunque pueda ser null: la gramatica permite
        # omitir propiedades opcionales y el modelo de 3B lo hacia siempre.
        "required": ["valor"],
    }


def _mensajes(espec: EspecSlot, texto: str) -> list[dict[str, str]]:
    sistema = (
        f"Clasifica cómo está {espec.pregunta} de un paciente operado, según lo que él dice.\n"
        f"Responde SOLO con uno de estos valores: {', '.join(espec.valores)}.\n"
        f"Si el paciente no dio información sobre esto, responde null. "
        f"Nunca supongas: null es una respuesta válida y preferible a inventar.\n\n"
        f"CRITERIO:\n{espec.criterio}\n\n"
        f"Extrae lo que el paciente REPORTA COMO HECHO, no su opinión sobre si es grave. "
        f"Si dice 'me salió pus pero creo que es normal', el hecho es que hay pus."
    )
    mensajes: list[dict[str, str]] = [{"role": "system", "content": sistema}]
    for enunciado, etiqueta in espec.ejemplos:
        mensajes.append({"role": "user", "content": enunciado})
        mensajes.append({"role": "assistant", "content": json.dumps({"valor": etiqueta})})
    mensajes.append({"role": "user", "content": texto})
    return mensajes


async def extraer(
    cliente: ClienteLLM, texto_paciente: str, slot_objetivo: str, *, modelo: str | None = None
) -> Extraccion:
    """Extrae lo que se pueda del enunciado del paciente."""
    resultado = Extraccion(slots=Slots())
    # El enunciado del paciente ES la evidencia. No hace falta que el modelo lo copie.
    resultado.evidencia = texto_paciente.strip()
    resultado.pregunta_del_paciente = detectar_pregunta(texto_paciente)

    # --- Capa deterministica ---
    # La temperatura se busca siempre, no solo cuando se pregunto por ella: un
    # paciente puede mencionar su fiebre mientras se le pregunta por la herida,
    # y ese dato dispara el nivel ROJO.
    temperatura = numeros.extraer_temperatura(texto_paciente)
    if temperatura and (slot_objetivo == "fiebre_c" or temperatura.seguro):
        resultado.slots.fiebre_c = temperatura.valor
        resultado.fuentes["fiebre_c"] = "parser"

    if slot_objetivo == "dolor_nrs":
        dolor = numeros.extraer_dolor(texto_paciente)
        if dolor:
            resultado.slots.dolor_nrs = dolor.valor
            resultado.fuentes["dolor_nrs"] = "parser"

    # --- Capa del modelo: solo si el slot objetivo es categorico ---
    espec = SLOTS.get(slot_objetivo)
    if espec is not None:
        respuesta = await cliente.chat(
            _mensajes(espec, texto_paciente),
            esquema=_esquema(espec),
            temperatura=0.0,
            max_tokens=24,
            modelo=modelo,
        )
        resultado.respuesta_llm = respuesta
        valor = _parsear(respuesta.texto).get("valor")
        if valor in espec.valores:
            setattr(resultado.slots, slot_objetivo, valor)
            resultado.fuentes[slot_objetivo] = "modelo"

        if slot_objetivo == "herida":
            piso = _piso_herida(texto_paciente)
            actual = resultado.slots.herida
            if piso and (actual is None or espec.valores.index(piso) > espec.valores.index(actual)):
                resultado.slots.herida = piso
                resultado.fuentes["herida"] = "piso_lexico" if actual is None else "piso_lexico(subio)"

    resultado.respondio = (
        getattr(resultado.slots, slot_objetivo, None) is not None
        and not _PATRON_EVASION.search(texto_paciente)
    )
    return resultado


def _parsear(texto: str) -> dict:
    """El esquema forzado hace raro el JSON invalido, pero no imposible: un
    fallo de parseo no puede tumbar la llamada, solo dejar el slot vacio."""
    try:
        datos = json.loads(texto)
        return datos if isinstance(datos, dict) else {}
    except json.JSONDecodeError:
        inicio, fin = texto.find("{"), texto.rfind("}")
        if inicio != -1 and fin > inicio:
            try:
                return json.loads(texto[inicio : fin + 1])
            except json.JSONDecodeError:
                return {}
        return {}
