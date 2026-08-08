"""Genera el PDF de demostración para la prueba de conocimiento vivo (G5).

    .venv/bin/python scripts/generar_pdf_demo.py

Su contenido es deliberadamente ajeno al corpus del reto: si el agente puede
responder sobre él, es porque lo leyó del documento y no porque lo supiera de
antes. Eso es exactamente lo que la compuerta G5 verifica.
"""

from __future__ import annotations

import sys
from pathlib import Path

SALIDA = Path(__file__).resolve().parents[1] / "demo" / "protocolo-zumbido-postoperatorio.pdf"

LINEAS = [
    "PROTOCOLO INSTITUCIONAL 7-B: ZUMBIDO POSTOPERATORIO",
    "",
    "Alcance: pacientes que refieren zumbido de oido en el postoperatorio",
    "inmediato de cirugia abdominal bajo anestesia general.",
    "",
    "1. Manejo inicial. Se indica reposo auditivo durante setenta y dos horas,",
    "evitando ambientes ruidosos y el uso de audifonos.",
    "",
    "2. Control. Se programa control ambulatorio a los siete dias con el",
    "servicio de otorrinolaringologia.",
    "",
    "3. Persistencia. Si el zumbido persiste mas de una semana se solicita",
    "audiometria tonal y se remite a valoracion especializada.",
    "",
    "4. Signos de alarma. La aparicion de vertigo, perdida subita de audicion",
    "o secrecion por el conducto auditivo obliga a valoracion inmediata.",
]


def construir_pdf(lineas: list[str]) -> bytes:
    """PDF minimo de una pagina. El /Length del flujo se calcula, no se supone:
    declararlo mal trunca el texto en la extraccion y el documento llega
    incompleto al indice."""
    contenido = ["BT", "/F1 11 Tf", "40 750 Td", "14 TL"]
    for linea in lineas:
        seguro = linea.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        contenido.append(f"({seguro}) Tj T*")
    contenido.append("ET")
    flujo = "\n".join(contenido).encode("latin-1")

    objetos = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(flujo)).encode() + b">>\nstream\n" + flujo + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]

    salida = bytearray(b"%PDF-1.4\n")
    desplazamientos = []
    for i, obj in enumerate(objetos, 1):
        desplazamientos.append(len(salida))
        salida += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"

    inicio_xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n".encode()
    salida += b"0000000000 65535 f \n"
    for desplazamiento in desplazamientos:
        salida += f"{desplazamiento:010d} 00000 n \n".encode()
    salida += (
        f"trailer\n<</Size {len(objetos) + 1}/Root 1 0 R>>\nstartxref\n{inicio_xref}\n%%EOF\n"
    ).encode()
    return bytes(salida)


def main() -> int:
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_bytes(construir_pdf(LINEAS))
    print(f"Escrito {SALIDA} ({SALIDA.stat().st_size} bytes)")

    # Verificar que el texto se extrae de verdad: un PDF que se abre pero del
    # que no se puede extraer texto haria fallar la demostracion en vivo.
    sys.path.insert(0, str(SALIDA.parents[1] / "src"))
    from postop.rag.ingest import extraer_paginas

    paginas, sin_texto = extraer_paginas(SALIDA)
    texto = paginas[0][1]
    print(f"Texto extraido: {len(texto)} caracteres, paginas sin capa de texto: {sin_texto}")
    assert "audiometria" in texto, "el texto no se extrajo completo"
    print("OK: el documento es indexable y contiene la respuesta esperada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
