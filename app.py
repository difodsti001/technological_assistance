"""
APLICACIÓN DEL AGENTE TECNOLÓGICO DIFODS
======================================
FastAPI principal. Módulo RAG de consultas sobre la plataforma SIFODS.
"""

import logging
import ssl
import certifi
import warnings
import asyncio
from datetime import datetime
from typing import Optional, List, Dict
from zoneinfo import ZoneInfo

import tiktoken
import psycopg2
from psycopg2 import pool as pg_pool

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from openai import AzureOpenAI
from google import genai
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

import os
import ssl

from src.settings import settings
from src.prompts import (
    PROMPT_BASE,
    PROMPT_SIFODS,
    MENSAJES_AYUDA,
)
from src.navegacion_router import NavegacionRouter

ssl._create_default_https_context = ssl.create_default_context(
    cafile=certifi.where()
)

logging.basicConfig(level=settings.servidor.log_level)
logger = logging.getLogger(__name__)

LIMA_TZ = ZoneInfo("America/Lima")

# ══════════════════════════════════════════════════════════════════════
# MODELOS PYDANTIC
# ══════════════════════════════════════════════════════════════════════
class SifodsRequest(BaseModel):
    mensaje:        str
    usuario:        str
    nombre_usuario: Optional[str] = None

# ══════════════════════════════════════════════════════════════════════
# APLICACIÓN FASTAPI
# ══════════════════════════════════════════════════════════════════════

app = FastAPI(
    title       = settings.agente.nombre,
    version     = settings.agente.version,
    description = settings.agente.descripcion,
)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ══════════════════════════════════════════════════════════════════════
# CLIENTES EXTERNOS
# ══════════════════════════════════════════════════════════════════════

openai_client = (
    AzureOpenAI(
        api_key=settings.llm.AZURE_API_KEY,
        api_version=settings.llm.AZURE_API_VERSION,
        azure_endpoint=settings.llm.AZURE_ENDPOINT,
    )
    if settings.llm.tiene_openai
    else None
)

gemini_model = None
if settings.llm.tiene_gemini:
    try:
        gemini_model = genai.Client(api_key=settings.llm.gemini_api_key)
    except Exception as e:
        logger.warning(f"⚠️  Gemini no disponible: {e}")

qdrant_client = QdrantClient(
    url     = settings.qdrant.url,
    api_key = settings.qdrant.api_key or None,
)

embedding_model = None

def get_embedding_model():
    global embedding_model

    if embedding_model is None:
        embedding_model = SentenceTransformer(
            settings.llm.embedding_model
        )

    return embedding_model


tokenizer       = tiktoken.encoding_for_model("gpt-4o-mini")

nav_router: Optional[NavegacionRouter] = None


# ══════════════════════════════════════════════════════════════════════
# CONNECTION POOL POSTGRESQL
# ══════════════════════════════════════════════════════════════════════

_db_pool: Optional[pg_pool.ThreadedConnectionPool] = None


def inicializar_pool() -> None:
    global _db_pool
    try:
        _db_pool = pg_pool.ThreadedConnectionPool(
            minconn = settings.db.pool_min,
            maxconn = settings.db.pool_max,
            **settings.db.as_dict(),
        )
        logger.info(
            f"✅ Connection pool PostgreSQL ({settings.db.pool_min}–{settings.db.pool_max})"
        )
    except Exception as e:
        logger.error(f"❌ No se pudo crear connection pool: {e}")
        _db_pool = None


def get_db_connection():
    if _db_pool:
        return _db_pool.getconn()
    return psycopg2.connect(**settings.db.as_dict())


def devolver_conexion(conn) -> None:
    if _db_pool and conn:
        _db_pool.putconn(conn)
    elif conn:
        conn.close()


def normalizar_texto(texto: str) -> str:
    return (texto or "").encode("utf-8", errors="ignore").decode("utf-8")

# ══════════════════════════════════════════════════════════════════════
# MEMORIA CONVERSACIONAL
# ══════════════════════════════════════════════════════════════════════

def obtener_memoria_conversacional(
    usuario: str,
    limite: int = 2
) -> List[Dict]:
    """
    Obtiene los últimos N intercambios del usuario.
    Usa la misma tabla conversaciones_agente.
    """

    conn = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT mensaje, respuesta
            FROM conversaciones_agente
            WHERE usuario = %s
            ORDER BY id DESC
            LIMIT %s
        """, (usuario, limite))

        rows = cur.fetchall()

        memoria = []

        # reverse para mantener orden cronológico
        for mensaje, respuesta in reversed(rows):

            if mensaje:
                memoria.append({
                    "role": "user",
                    "content": mensaje
                })

            if respuesta:
                memoria.append({
                    "role": "assistant",
                    "content": respuesta
                })

        return memoria

    except Exception as e:
        logger.warning(f"⚠️ Error obteniendo memoria conversacional: {e}")
        return []

    finally:
        devolver_conexion(conn)

# ══════════════════════════════════════════════════════════════════════
# LLM CON FALLBACK (GPT → Gemini)
# ══════════════════════════════════════════════════════════════════════

async def llamar_llm_con_fallback(messages: List[Dict], model_params: dict) -> str:
    loop = asyncio.get_running_loop()

    if openai_client:
        try:
            def _openai():
                return openai_client.chat.completions.create(
                    model       = settings.llm.AZURE_DEPLOYMENT,
                    messages = messages,
                    max_tokens  = model_params["max_tokens"],
                    temperature = model_params["temperature"],
                )
            resp  = await loop.run_in_executor(None, _openai)
            texto = resp.choices[0].message.content.strip()
            logger.info(f"✅ OpenAI | tokens_salida: {resp.usage.completion_tokens} | finish: {resp.choices[0].finish_reason}")
            return texto
        except Exception as e:
            logger.warning(f"⚠️  OpenAI falló: {e} → Gemini...")

    if gemini_model:
        try:
            from google.genai import types as genai_types

            def _gemini():
                cfg = genai_types.GenerateContentConfig(
                    temperature       = model_params["temperature"],
                    max_output_tokens = model_params["max_tokens"],
                    top_p             = model_params.get("top_p") or None,
                )
                contenido_gemini = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in messages])
                return gemini_model.models.generate_content(
                    model    = settings.llm.modelo_fallback,
                    contents = contenido_gemini,
                    config   = cfg,
                )
            logger.info(f"🔍 Gemini config → max_output_tokens: {model_params['max_tokens']} | temperature: {model_params['temperature']}")
            resp  = await loop.run_in_executor(None, _gemini)
            texto = resp.text.strip()
            finish = getattr(resp.candidates[0], "finish_reason", "?") if resp.candidates else "?"
            logger.info(f"✅ Gemini | chars: {len(texto)} | finish: {finish}")
            return texto
        except Exception as e2:
            logger.error(f"❌ Gemini también falló: {e2}")

    raise HTTPException(status_code=503, detail="LLM no disponible temporalmente")



# ══════════════════════════════════════════════════════════════════════
# QDRANT
# ══════════════════════════════════════════════════════════════════════

def search_qdrant(query: str) -> List[Dict]:
    try:
        emb = get_embedding_model().encode(query).tolist()
        result = qdrant_client.query_points(
            collection_name = settings.qdrant.coleccion,
            query           = emb,
            limit           = settings.qdrant.top_k,
        )
        return [
            {
                "text":     p.payload.get("text", ""),
                "score":    p.score,
                "filename": p.payload.get("filename", ""),
                "chunk":    p.payload.get("chunk", 0),
            }
            for p in result.points
        ]
    except Exception as e:
        logger.error(f"Error en Qdrant: {e}")
        return []


def _formatear_chunk(chunk: Dict) -> str:
    return f"[{chunk.get('filename', 'Documento')}]\n{chunk.get('text', '')}\n"


# ══════════════════════════════════════════════════════════════════════
# CLASIFICACIÓN DE TAREAS
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
# TAREA 1: CONSULTAS SIFODS
# ══════════════════════════════════════════════════════════════════════
async def reescribir_query_con_memoria(
    mensaje: str,
    memoria: List[Dict]
) -> str:

    if not memoria:
        return mensaje

    historial = "\n".join([
        f"{m['role'].upper()}: {m['content']}"
        for m in memoria[-4:]
    ])

    prompt = f"""
Historial conversacional:
{historial}

Pregunta actual:
{mensaje}

Si la pregunta depende del contexto anterior,
reescríbela para que sea completamente clara
y entendible por sí sola.

Si ya es clara, devuélvela igual.
Responde SOLO la pregunta reescrita.
"""

    try:

        if openai_client:

            response = openai_client.chat.completions.create(
                model=settings.llm.AZURE_DEPLOYMENT,
                messages=[{
                    "role": "user",
                    "content": prompt
                }],
                max_tokens=80,
                temperature=0
            )

            nueva_query = response.choices[0].message.content.strip()

        elif gemini_model:

            from google.genai import types as genai_types

            cfg = genai_types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=80,
            )

            response = gemini_model.models.generate_content(
                model=settings.llm.modelo_fallback,
                contents=prompt,
                config=cfg,
            )

            nueva_query = response.text.strip()

        else:
            return mensaje

        if nueva_query:
            return nueva_query

    except Exception as e:
        logger.warning(f"⚠️ Error reescribiendo query: {e}")

    return mensaje

async def procesar_consulta_sifods(mensaje: str, usuario: str) -> Dict:
    ts = datetime.now(LIMA_TZ)

    memoria = obtener_memoria_conversacional(usuario, limite=2)
    mensaje_rag = await reescribir_query_con_memoria(mensaje, memoria)

    # Routing híbrido: selecciona la colección correcta antes de buscar
    if nav_router:
        routing = nav_router.route(mensaje_rag)
        chunks  = nav_router.search(mensaje_rag)
        nodo_info = f"{routing['node']} ({routing['kb']})"
        fuente    = f"qdrant:{routing['kb']}"
    else:
        # fallback: comportamiento anterior si el router no está disponible
        chunks    = search_qdrant(mensaje_rag)
        nodo_info = "general"
        fuente    = "qdrant"

    if not chunks:
        return {
            "respuesta":    MENSAJES_AYUDA["sin_resultados_sifods"],
            "tarea":        "sifods",
            "fuente_datos": "ninguna",
            "referencias":  [],
        }

    context   = "\n\n".join(_formatear_chunk(c) for c in chunks)
    memoria = obtener_memoria_conversacional(usuario, limite=2)
    system_prompt = (
        PROMPT_BASE.format(
            context=context,
            question=mensaje
        )
        + "\n\n" +
        PROMPT_SIFODS
        + "\n\nUsa el historial conversacional para mantener continuidad "
        "cuando el usuario haga referencias a mensajes anteriores."
    )
    messages = [
        {
            "role": "system",
            "content": system_prompt
        }
    ]
    messages.extend(memoria)
    messages.append({
        "role": "user",
        "content": mensaje
    })
    respuesta = await llamar_llm_con_fallback(messages, settings.sifods.parametros_modelo)
    latencia  = int((datetime.now(LIMA_TZ) - ts).total_seconds() * 1000)
    texto_tokens = "\n".join([ f"{m['role']}: {m['content']}"for m in messages])

    return {
        "respuesta":      respuesta,
        "tarea":          "sifods",
        "fuente_datos":   fuente,
        "nodo_navegacion": nodo_info,                 # ← nuevo campo de trazabilidad
        "referencias":    [{"fuente": c["filename"], "relevancia": c["score"]} for c in chunks[:3]],
        "tokens_entrada": len(tokenizer.encode(texto_tokens)),
        "tokens_salida":  len(tokenizer.encode(respuesta)),
        "latencia_ms":    latencia,
    }

# ══════════════════════════════════════════════════════════════════════
# PERSISTENCIA
# ══════════════════════════════════════════════════════════════════════

def _guardar(usuario, nombre, mensaje, respuesta, tarea, fuente, te, ts_tok, lat) -> Optional[int]:
    """
    Inserta en conversaciones_agente y retorna el id generado.
    Retorna None si guardar_conversaciones=False o si falla.
    """
    if not settings.servidor.guardar_conversaciones:
        return None
    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO conversaciones_agente
            (usuario, nombre_usuario, mensaje, respuesta, tarea,
             fuente_datos, tokens_entrada, tokens_salida, latencia_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            normalizar_texto(usuario),
            normalizar_texto(nombre or ""),
            normalizar_texto(mensaje),
            normalizar_texto(respuesta),
            tarea, fuente, te, ts_tok, lat,
        ))
        conversacion_id = cur.fetchone()[0]
        conn.commit()
        return conversacion_id
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.warning(f"No se pudo guardar conversación: {e}")
        return None
    finally:
        devolver_conexion(conn)




def guardar_conversacion_sifods(usuario: str, nombre: str, mensaje: str, resultado: Dict) -> None:
    _guardar(
        usuario, nombre,
        mensaje, resultado["respuesta"],
        resultado["tarea"], resultado.get("fuente_datos"),
        resultado.get("tokens_entrada"), resultado.get("tokens_salida"),
        resultado.get("latencia_ms"),)


# ══════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ══════════════════════════════════════════════════════════════════════
def verificar_schema() -> bool:
    """
    Verifica que las tablas operacionales del agente existan en la BD.
    NO crea ni modifica nada — solo informa.

    Las tablas de negocio (inscripciones, cursos, docentes) son externas
    y se configuran en .env — no son responsabilidad de este check.

    Retorna True si todo está OK, False si falta algo.
    """
    TABLAS_AGENTE  = ["conversaciones_agente"]
    VISTAS_AGENTE  = ["v_metricas_diarias"]

    conn = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s",
            (settings.db.schema,)
        )
        tablas_existentes = {row[0] for row in cur.fetchall()}

        cur.execute(
            "SELECT viewname FROM pg_views WHERE schemaname = %s",
            (settings.db.schema,)
        )
        vistas_existentes = {row[0] for row in cur.fetchall()}

        tablas_faltantes = [t for t in TABLAS_AGENTE if t not in tablas_existentes]
        vistas_faltantes = [v for v in VISTAS_AGENTE  if v not in vistas_existentes]
        todo_faltante    = tablas_faltantes + vistas_faltantes

        if not todo_faltante:
            logger.info("✅ BD verificada — tablas operacionales OK")
            return True

        logger.error(
            f"❌ Faltan objetos en la BD: {todo_faltante}\n"
            f"   Ejecuta el schema antes de iniciar el agente."
        )
        return False

    except Exception as e:
        logger.error(f"❌ No se pudo verificar la BD: {e}")
        return False
    finally:
        devolver_conexion(conn)
        
@app.on_event("startup")
async def startup_event():
    global nav_router
    inicializar_pool()

    logger.info("🔄 Inicializando router de navegación...")
    try:
        nav_router = NavegacionRouter(
            qdrant_client=qdrant_client,
            settings=settings,
            gemini_client=gemini_model,
        )
        logger.info("✅ Router de navegación activo")
    except Exception as e:
        logger.error(f"❌ Router de navegación no disponible: {e}")
        nav_router = None

    logger.info(
        f"🚀 {settings.agente.nombre} v{settings.agente.version} | "
        f"Puerto: {settings.servidor.port} | "
        f"NavRouter: {'✅ activo' if nav_router else '⚠️  no disponible'} | "
        f"Gemini: {'✅' if gemini_model else '⚠️  no configurado'}"
    )


@app.on_event("shutdown")
async def shutdown_event():
    if _db_pool:
        _db_pool.closeall()
        logger.info("🔒 Connection pool cerrado")

# ══════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════

@app.get("/agente_tecnologico", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        name="agente_tecnologico.html",
        request=request,
        context={
            "request": request,
            "agente": {
                "nombre": settings.agente.nombre,
                "version": settings.agente.version,
                "descripcion": settings.agente.descripcion,
                "emoji": settings.agente.emoji,
                "id_agente": settings.agente.id_agente,
            }
        },
    )


@app.get("/health")
async def health_check():
    return {
        "status":         "healthy",
        **settings.resumen(),
        "db_pool_activo": _db_pool is not None,
        "timestamp":      datetime.now(LIMA_TZ).isoformat(),
    }


@app.post("/api/sifods")
async def consulta_sifods(request:SifodsRequest):
    """
    Módulo RAG — responde preguntas sobre la plataforma SIFODS.
    Busca contexto en Qdrant y genera respuesta con el LLM.
    """
    try:
        resultado = await procesar_consulta_sifods(request.mensaje, request.usuario)
        guardar_conversacion_sifods(request.usuario, request.nombre_usuario, request.mensaje, resultado)
        return {
            "respuesta":    resultado["respuesta"],
            "fuente_datos": resultado.get("fuente_datos"),
            "referencias":  resultado.get("referencias", []),
            "metadata": {
                "tokens_entrada": resultado.get("tokens_entrada"),
                "tokens_salida":  resultado.get("tokens_salida"),
                "latencia_ms":    resultado.get("latencia_ms"),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error en /api/sifods: {e}")
        raise HTTPException(status_code=500, detail=str(e))



@app.get("/api/config")
async def ver_config():
    """
    Muestra la configuración activa (sin secrets).
    Útil para depuración en desarrollo.
    """
    return {
        **settings.resumen(),
        "sifods": {
            "max_tokens":  settings.sifods.max_tokens,
            "temperature": settings.sifods.temperature,
        },
        "db": {
            "host":   settings.db.host,
            "name":   settings.db.name,
            "schema": settings.db.schema,
        },
    }


# ══════════════════════════════════════════════════════════════════════
# SERVIDOR
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host   = "0.0.0.0",
        port   = settings.servidor.port,
        reload = False,
    )
