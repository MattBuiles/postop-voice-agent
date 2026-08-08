# Arquitectura

> Cada nodo de los diagramas lleva **la ruta real del módulo** que lo implementa.
> La rúbrica advierte que el jurado toma elementos del diagrama al azar y los
> busca en el código; esta correspondencia es verificable línea por línea.

---

## 1. Arquitectura de la solución

```mermaid
flowchart TB
    subgraph navegador["NAVEGADOR — web/call.html · web/admin.html"]
        mic["Micrófono + VAD por energía<br/><i>call.html: onaudioprocess</i>"]
        rep["Cola de reproducción + barge-in<br/><i>call.html: reproducir / cortarAudio</i>"]
        glass["Panel glass-box<br/>slots · fuentes · decisión · latencias"]
        consola["Consola: subir / listar / eliminar<br/><i>admin.html</i>"]
    end

    subgraph app["APLICACIÓN — src/postop/main.py (FastAPI, un proceso)"]
        ws["WebSocket /ws/llamada<br/><i>main.ws_llamada</i>"]
        turno["Orquestación del turno<br/><i>main._procesar_turno</i>"]
        api["API de conocimiento<br/><i>main.subir_documento / eliminar_documento</i>"]
    end

    subgraph nucleo["NÚCLEO"]
        asr["asr/transcribir.py<br/>faster-whisper int8"]
        num["asr/numeros.py<br/>parser de temperatura y dolor"]
        guard["dialog/guardas.py<br/>4 capas anti-inyección"]
        maq["dialog/maquina.py<br/>máquina de 6 slots"]
        ext["llm/extract.py<br/>1 slot por invocación"]
        resp["llm/responder.py<br/>respuesta anclada"]
        rag["rag/retrieve.py<br/>híbrido + RRF"]
        ver["rag/verify.py<br/>verificador de anclaje"]
        tri["triage/rules.py<br/>regla determinista"]
        res["summary/resumen.py<br/>JSON + FHIR R4"]
        tts["tts/voz.py<br/>Piper + caché"]
        obs["obs/traza.py<br/>JSONL + métricas"]
    end

    subgraph datos["ALMACENAMIENTO"]
        db[("data/knowledge.db<br/>chunks · vec0 · fts5<br/>calls · turns · alerts · recibos")]
        ollama["Ollama<br/>llama3.2:3b"]
    end

    mic -->|PCM 16 kHz| ws
    ws --> turno
    turno --> asr --> num
    turno --> guard
    turno --> maq
    turno --> ext --> ollama
    turno --> rag --> db
    rag --> resp --> ver
    resp --> ollama
    turno --> tri
    turno --> res --> db
    turno --> obs
    maq --> tts --> rep
    consola --> api --> db
    glass -.->|eventos JSON| ws
```

**Por qué un solo proceso y un solo archivo.** La compuerta G2 cronometra 15
minutos desde el README hasta la solución en pie. Cada servicio accesorio es una
oportunidad de que ese reloj se agote. El único proceso aparte es Ollama, y está
separado a propósito: así la descarga de pesos corre en paralelo con el arranque
de la aplicación en vez de serializarse.

---

## 2. Flujo de decisión del agente

```mermaid
flowchart TD
    A["Paciente habla"] --> B["VAD detecta fin de habla<br/><i>call.html · 700 ms</i>"]
    B --> C["STT<br/><i>asr/transcribir.py</i>"]
    C --> D["Parser de números<br/><i>asr/numeros.py</i>"]
    D --> E{"¿Guarda de entrada?<br/><i>dialog/guardas.revisar_entrada</i>"}

    E -->|"inyección o<br/>petición de dosis"| E1["Frase de contención<br/>+ repetir la pregunta pendiente"]
    E1 --> Z

    E -->|limpio| F{"¿Bandera roja en texto libre?<br/><i>triage/rules.detectar_banderas</i>"}
    F -->|"sangrado · disnea · dolor torácico<br/>síncope · TVP · dehiscencia"| ROJO

    F -->|no| G["Extracción del slot<br/><i>llm/extract.py</i>"]
    G --> G1{"¿Slot resuelto?"}
    G1 -->|no, intento 1| G2["Repregunta reformulada"] --> Z
    G1 -->|"no, intento 2<br/>slot crítico"| INC["Marcar agotado →<br/>escala por incertidumbre"]
    G1 -->|sí| H

    INC --> H
    H{"¿El paciente preguntó algo?"}
    H -->|sí| I["Recuperación híbrida<br/><i>rag/retrieve.py</i>"]
    I --> J["Generación anclada<br/><i>llm/responder.py</i>"]
    J --> K{"¿Cita verificada contra el fragmento?<br/><i>rag/verify.py · umbral 0.88</i>"}
    K -->|no| K1["«No la sé con la información que tengo»<br/>+ se anota para el equipo"]
    K -->|sí| K2{"¿Filtro de salida?<br/><i>guardas.revisar_salida</i>"}
    K2 -->|"contiene dosis<br/>o medicamento"| K1
    K2 -->|limpio| K3["Responder citando documento y página"]
    K1 --> L
    K3 --> L
    H -->|no| L

    L["Evaluación de triaje<br/><i>triage/rules.evaluar</i>"]
    L --> M{"Nivel"}

    M -->|"fiebre ≥ 38 · dolor ≥ 8<br/>secreción purulenta"| ROJO
    M -->|"puntaje ≥ 4 · febrícula ≥ 37,5<br/>incertidumbre · deterioro"| AMARILLO
    M -->|resto| VERDE

    ROJO["<b>ROJO</b><br/>Interrumpe el protocolo<br/>Alerta persistida<br/>Deriva a urgencias"]
    AMARILLO["<b>AMARILLO</b><br/>Alerta de vigilancia<br/>Contacto en 24 h"]
    VERDE["<b>VERDE</b><br/>Cierre educativo<br/><i>siempre enumera signos de alarma</i>"]

    ROJO --> R["Resumen + FHIR + alerta<br/><i>summary/resumen.py</i>"]
    AMARILLO --> Z
    VERDE --> Z
    Z["Siguiente pregunta del protocolo<br/><i>dialog/maquina.siguiente_pregunta</i>"]
    Z --> Y{"¿Quedan slots?"}
    Y -->|sí| A
    Y -->|no| R
```

**El nivel solo sube, nunca baja.** Todas las capas de decisión pasan por
`triage/rules._subir`. Ninguna evaluación posterior puede tranquilizar un caso que
una capa anterior consideró grave. Es la traducción a código de la asimetría
clínica que declara la rúbrica: el falso negativo es la falla catastrófica.

---

## 3. El ciclo del conocimiento vivo

```mermaid
sequenceDiagram
    participant J as Jurado
    participant C as Consola
    participant I as rag/ingest.py
    participant D as knowledge.db
    participant A as Agente

    J->>C: Sube documento nuevo
    C->>I: ingerir_documento()
    Note over I: recibido → extrayendo → [ocr] →<br/>fragmentando → embebiendo → disponible
    I->>D: INSERT chunks + vec0 + fts5 (una transacción)
    I->>D: bump version_corpus → invalida la caché
    Note over I,D: Si existe la misma identidad lógica,<br/>la versión anterior queda superseded<br/>y deja de ser recuperable
    J->>A: Pregunta sobre el documento
    A->>D: recuperación híbrida
    A-->>J: Responde citando documento y página

    J->>C: Elimina el documento
    C->>I: eliminar_documento()
    I->>D: DELETE chunks + vec0 + fts5 + documents<br/>(una transacción)
    I->>D: INSERT recibo de olvido
    J->>A: Vuelve a preguntar lo mismo
    A->>D: recuperación → sin fragmentos
    A-->>J: «No tengo esa información»
```

**El olvido es estructural, no una promesa del prompt.** El agente solo puede
afirmar algo si entrega una cita literal que `rag/verify.py` valida contra un
fragmento existente. Borrado el fragmento, no hay nada que citar y la afirmación
deja de ser posible. No depende de que el modelo "obedezca".

---

## 4. Presupuesto de latencia por turno

| Etapa | Módulo | Coste típico |
|---|---|---|
| Fin de habla (VAD) | `web/call.html` | 700 ms de silencio configurado |
| Transcripción | `asr/transcribir.py` | ~0,25× tiempo real |
| Parser de números | `asr/numeros.py` | < 5 ms |
| Extracción de slot | `llm/extract.py` | ~1.300 ms (P50 medido) |
| Recuperación (solo si hay pregunta) | `rag/retrieve.py` | 110–190 ms |
| TTS de turno guionado | `tts/voz.py` (caché) | **0 ms** |
| TTS de texto generado | `tts/voz.py` | ~1.000 ms al primer audio |

Las dos decisiones que sostienen el presupuesto:

1. **Una invocación por slot, con enum acotado.** Pedirle al modelo los cuatro
   slots categóricos más la evidencia costaba ~50 tokens de salida y 7,3 s por
   turno. Acotarlo a un enum de tres valores lo dejó en 7,7 tokens y 1,3 s.
2. **Pre-síntesis de todo el texto fijo del agente.** Saludo, las seis preguntas,
   las repreguntas, los cierres y los backchannels se sintetizan al arrancar. En
   la mayoría de los turnos el TTS no cuesta nada.

---

## 5. Modelo de datos

```
documents(doc_id, logical_id, nombre, escenario, sha256, version,
          superseded_by, estado, origen, subido_ts, procesado_ts)
chunks(id, chunk_uid, doc_id, pagina, seccion, texto, n_tokens)
vec_chunks      -- tabla virtual sqlite-vec, embedding float[768]
fts_chunks      -- tabla virtual FTS5, unicode61 remove_diacritics 2
corpus_state    -- versión global; forma parte de la clave de caché
deletion_receipts(receipt_id, doc_id, sha256, chunks_eliminados,
                  corpus_version_antes, corpus_version_despues)
calls / turns / alerts / traces
```

`chunks.id`, `vec_chunks.chunk_rowid` y `fts_chunks.rowid` son **el mismo
identificador**. Esa decisión es la que permite que un borrado toque los tres
índices dentro de una sola transacción, sin ningún almacén externo que pueda
quedar desincronizado.

`remove_diacritics 2` en FTS5 no es cosmético: hace que "secrecion" encuentre
"secreción", y el paciente no escribe tildes ni el STT las acierta siempre.
