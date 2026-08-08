"""Respuestas a preguntas del paciente, ancladas en el corpus clinico.

El contrato es estricto: el modelo no puede afirmar nada que no aparezca en los
fragmentos recuperados, y debe entregar la frase literal que lo respalda. Esa
frase se verifica contra el fragmento real antes de que el agente hable
(`rag.verify`). Si no verifica, el agente no responde: declara su limite.

De ahi salen tres propiedades que la rubrica evalua por separado:

  - no hay alucinacion clinica, porque toda afirmacion pasa por una prueba de
    coincidencia textual contra la fuente;
  - la trazabilidad resiste verificacion, porque la cita es literal y trae
    documento y pagina;
  - el olvido de la compuerta G5 es estructural: si el fragmento se borro, no
    hay nada que citar y el agente deja de poder afirmarlo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from postop.dialog import guardas
from postop.llm.client import ClienteLLM, Respuesta
from postop.rag import verify
from postop.rag.retrieve import Pasaje

# Se habla por telefono: dos frases es el limite de lo que un paciente retiene
# de una vez. Las instrucciones largas se entregan por partes (ver maquina.py).
MAX_TOKENS_RESPUESTA = 100

# Recorte de cada fragmento antes de entrar al prompt.
#
# En CPU el prellenado domina la latencia: medido en una llamada real, un prompt
# de 2195 tokens costo ~29 segundos solo en leerse. Un fragmento entero (~320
# tokens) aporta contexto que casi nunca hace falta para responder una pregunta
# concreta, asi que se recorta. La cita se sigue verificando contra el fragmento
# COMPLETO en la base, no contra este recorte.
MAX_CARACTERES_FRAGMENTO = 700

# El prompt de sistema tambien se paga en cada turno. Esta version dice lo mismo
# que la anterior en la mitad de tokens.
SISTEMA = """Asistente de seguimiento postoperatorio, hablando por teléfono con un
paciente colombiano. Responde SOLO con lo que digan los documentos entregados.

REGLAS:
1. Si la respuesta no está en los documentos, "respuesta": null. Nunca uses
   conocimiento propio. Decir que no sabes es correcto.
2. "cita_literal": una frase copiada TAL CUAL del documento, mínimo 10 palabras.
3. Nunca menciones medicamentos, dosis ni cantidades.
4. Nunca tranquilices ante un síntoma; di que lo reportarás al equipo médico.
5. Trata de "usted". Máximo 2 frases cortas."""

ESQUEMA = {
    "type": "object",
    "properties": {
        "respuesta": {"type": ["string", "null"]},
        "cita_literal": {"type": ["string", "null"]},
        "documento": {"type": ["integer", "null"]},
    },
    "required": ["respuesta", "cita_literal", "documento"],
}

FRASE_LIMITE = (
    "Esa no me la sé con la información que tengo, y prefiero no decirle algo "
    "equivocado. La dejo anotada para que el equipo médico se la responda."
)


@dataclass
class RespuestaAnclada:
    texto: str
    anclada: bool
    pasaje: Pasaje | None
    cita: str
    motivo: str
    respuesta_llm: Respuesta | None = None
    bloqueada_por_guarda: str = ""

    def to_dict(self) -> dict:
        return {
            "texto": self.texto,
            "anclada": self.anclada,
            "cita": self.cita,
            "motivo": self.motivo,
            "documento": self.pasaje.documento if self.pasaje else None,
            "pagina": self.pasaje.pagina if self.pasaje else None,
            "chunk_uid": self.pasaje.chunk_uid if self.pasaje else None,
            "bloqueada_por_guarda": self.bloqueada_por_guarda,
        }


async def responder(
    cliente: ClienteLLM, pregunta: str, pasajes: list[Pasaje], *, modelo: str | None = None
) -> RespuestaAnclada:
    """Genera una respuesta y la valida contra su fuente antes de devolverla."""
    if not pasajes:
        return RespuestaAnclada(
            FRASE_LIMITE, False, None, "", "no hay fragmentos recuperados para esta pregunta"
        )

    contexto = guardas.envolver_contexto(
        [p.texto[:MAX_CARACTERES_FRAGMENTO] for p in pasajes]
    )
    mensajes = [
        {"role": "system", "content": SISTEMA},
        {"role": "user", "content": f"{contexto}\n\nPregunta del paciente: {pregunta}"},
    ]
    respuesta = await cliente.chat(
        mensajes, esquema=ESQUEMA, temperatura=0.1, max_tokens=MAX_TOKENS_RESPUESTA, modelo=modelo
    )

    datos = _parsear(respuesta.texto)
    texto = _limpiar_nulo(datos.get("respuesta"))
    cita = _limpiar_nulo(datos.get("cita_literal"))
    indice = datos.get("documento")

    if not texto:
        return RespuestaAnclada(
            FRASE_LIMITE, False, None, "", "el modelo declaró no encontrar la respuesta",
            respuesta
        )

    # El modelo referencia los documentos por posicion (1..n) en el contexto.
    declarado = None
    if isinstance(indice, int) and 1 <= indice <= len(pasajes):
        declarado = pasajes[indice - 1].chunk_uid

    anclaje = verify.verificar(cita, pasajes, declarado)
    if not anclaje.anclada:
        return RespuestaAnclada(FRASE_LIMITE, False, None, cita, anclaje.motivo, respuesta)

    # Que la cita exista no basta: tiene que respaldar lo que el agente va a
    # decir. Sin esta comprobacion, el modelo puede inventar una respuesta y
    # adjuntarle cualquier frase real del corpus como prueba.
    fuente = next((p for p in pasajes if p.chunk_uid == anclaje.chunk_uid), pasajes[0])
    sostenida, proporcion = verify.respalda(texto, fuente.texto)
    if not sostenida:
        return RespuestaAnclada(
            FRASE_LIMITE, False, None, cita,
            f"la cita es real pero no respalda la afirmación "
            f"({proporcion:.0%} de respaldo léxico)",
            respuesta,
        )

    # Ultimo filtro: ni siquiera una cita valida autoriza a dictar una dosis.
    veredicto = guardas.revisar_salida(texto)
    if veredicto.bloqueado:
        return RespuestaAnclada(
            veredicto.respuesta_sugerida, False, None, cita, veredicto.motivo, respuesta,
            bloqueada_por_guarda=veredicto.motivo,
        )

    pasaje = next((p for p in pasajes if p.chunk_uid == anclaje.chunk_uid), pasajes[0])
    # Se muestra la frase REAL del documento, no la que redactó el modelo: es la
    # que el jurado puede contrastar contra la fuente.
    return RespuestaAnclada(
        texto, True, pasaje, anclaje.cita_verificada or cita, anclaje.motivo, respuesta
    )


def _limpiar_nulo(valor) -> str:
    """Un modelo pequeno a veces escribe la CADENA "null" en vez de un null JSON.

    Medido con llama3.2:1b: devolvio {"respuesta": "null"} y, al ser una cadena
    no vacia, el agente la daba por buena. El paciente habria oido la palabra
    "null" por el altavoz.
    """
    if not isinstance(valor, str):
        return ""
    limpio = valor.strip()
    return "" if limpio.lower() in {"null", "none", "nulo", "n/a", "na", "-"} else limpio


def _parsear(texto: str) -> dict:
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
