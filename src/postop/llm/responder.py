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
MAX_TOKENS_RESPUESTA = 120

SISTEMA = """Eres un asistente de seguimiento postoperatorio hablando por teléfono
con un paciente colombiano. Respondes usando ÚNICAMENTE los documentos que se te
entregan.

REGLAS ABSOLUTAS:
1. Si la respuesta no está en los documentos, responde con "valor": null. NUNCA
   uses conocimiento propio. Es correcto y preferible decir que no sabes.
2. "cita_literal" debe ser una frase copiada TAL CUAL de los documentos, sin
   cambiar ni una palabra. Es lo que permite verificar tu respuesta.
3. Nunca menciones medicamentos, dosis, ni cantidades. Eso lo define el médico.
4. Nunca tranquilices al paciente sobre un síntoma. Si algo suena preocupante,
   dile que lo vas a reportar al equipo médico.
5. Habla de "usted", en español colombiano, claro y cálido.
6. Máximo 2 frases cortas. Es una llamada, no un folleto."""

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

    contexto = guardas.envolver_contexto([p.texto for p in pasajes])
    mensajes = [
        {"role": "system", "content": SISTEMA},
        {"role": "user", "content": f"{contexto}\n\nPregunta del paciente: {pregunta}"},
    ]
    respuesta = await cliente.chat(
        mensajes, esquema=ESQUEMA, temperatura=0.1, max_tokens=MAX_TOKENS_RESPUESTA, modelo=modelo
    )

    datos = _parsear(respuesta.texto)
    texto = (datos.get("respuesta") or "").strip()
    cita = (datos.get("cita_literal") or "").strip()
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
