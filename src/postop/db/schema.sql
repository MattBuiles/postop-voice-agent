-- Esquema unico del sistema. Todo vive en un solo archivo SQLite para que el
-- borrado de un documento sea ATOMICO: vectores, indice lexico, metadatos y
-- cache caen en la misma transaccion. Eso es lo que hace demostrable la
-- compuerta G5 ("lo eliminas y el agente lo olvida"): no hay ningun otro
-- almacen donde pueda sobrevivir una copia.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- conocimiento

CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    -- Identidad logica estable entre versiones: subir "protocolo-dolor v2"
    -- supersede a "protocolo-dolor v1" en vez de convivir con el.
    logical_id    TEXT NOT NULL,
    nombre        TEXT NOT NULL,
    escenario     TEXT,                       -- appendicitis | cholecystitis | ...
    sha256        TEXT NOT NULL,
    n_paginas     INTEGER DEFAULT 0,
    version       INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT REFERENCES documents(doc_id) ON DELETE SET NULL,
    -- recibido -> extrayendo -> ocr -> fragmentando -> embebiendo -> disponible | error
    estado        TEXT NOT NULL DEFAULT 'recibido',
    error         TEXT,
    origen        TEXT NOT NULL DEFAULT 'upload',   -- corpus | upload
    subido_ts     TEXT NOT NULL,
    procesado_ts  TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_logical ON documents(logical_id, version);
CREATE INDEX IF NOT EXISTS idx_documents_estado ON documents(estado);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY,             -- rowid compartido con vec_chunks y fts_chunks
    chunk_uid TEXT UNIQUE NOT NULL,            -- identificador citable y estable
    doc_id    TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    pagina    INTEGER NOT NULL,
    seccion   TEXT,
    texto     TEXT NOT NULL,
    n_tokens  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- Version global del corpus. Cualquier alta o baja la incrementa, y la clave
-- del cache de recuperacion la incluye: por eso un documento borrado no puede
-- reaparecer desde una respuesta cacheada.
CREATE TABLE IF NOT EXISTS corpus_state (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO corpus_state (id, version) VALUES (1, 0);

-- Evidencia auditable de los borrados: el "recibo de olvido" de G5.
CREATE TABLE IF NOT EXISTS deletion_receipts (
    receipt_id       TEXT PRIMARY KEY,
    doc_id           TEXT NOT NULL,
    nombre           TEXT NOT NULL,
    sha256           TEXT NOT NULL,
    chunks_eliminados INTEGER NOT NULL,
    corpus_version_antes INTEGER NOT NULL,
    corpus_version_despues INTEGER NOT NULL,
    eliminado_ts     TEXT NOT NULL
);

-- ------------------------------------------------------------------- llamadas

CREATE TABLE IF NOT EXISTS calls (
    call_id      TEXT PRIMARY KEY,
    paciente_id  TEXT,
    procedimiento TEXT,
    dia_postop   INTEGER,
    inicio_ts    TEXT NOT NULL,
    fin_ts       TEXT,
    triaje_final TEXT,                         -- verde | amarillo | rojo
    resumen_json TEXT,
    fhir_json    TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id     TEXT PRIMARY KEY,
    call_id     TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    hablante    TEXT NOT NULL,                 -- agente | paciente | tercero
    texto       TEXT NOT NULL,
    slots_json  TEXT,
    citas_json  TEXT,
    latencias_json TEXT,
    tokens_json TEXT,
    ts          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_call ON turns(call_id, idx);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id    TEXT PRIMARY KEY,
    call_id     TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    nivel       TEXT NOT NULL,
    motivo_json TEXT NOT NULL,                 -- que regla disparo y con que valores
    evidencias_json TEXT NOT NULL,             -- frases textuales del paciente que la sustentan
    estado      TEXT NOT NULL DEFAULT 'abierta',  -- abierta | reconocida | cerrada
    creado_ts   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_estado ON alerts(estado, creado_ts);

CREATE TABLE IF NOT EXISTS traces (
    trace_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT,
    evento       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traces_call ON traces(call_id, trace_id);
