# 🤖 Agente Tecnológico DIFODS — Documentación de Estructura

## Estructura del Proyecto

```
agente_tecnologico/
│
├── .env                          ← Variables de entorno (NO commitear)
├── .env.example                  ← Plantilla de .env (sí commitear)
│
├── app.py                        ← FastAPI: endpoints, orquestación
│
├── config/
│   ├── __init__.py               ← Exporta `settings`
│   ├── settings.py               
│   └── prompts.py                ← Todos los prompts del sistema
│
├── templates/
│   └── agente_tecnologico.html   ← Frontend HTML
```

---

## Flujo de Configuración

```
.env
 └─→ config/settings.py (Settings dataclasses)
       ├─→ app.py               (usa settings.*)
       ├─→ sistema_recomendacion.py (usa settings.recomendacion, settings.llm)
       └─→ config/prompts.py    (plantillas de texto, sin parámetros)
```

**Regla**: ningún archivo fuera de `config/settings.py` debe leer `os.getenv()` directamente.

---


---

## Bug Corregido: top_k Ignorado

**Problema original**: `obtener_recomendaciones()` en `sistema_recomendacion.py`
tenía `top_k=5` hardcodeado. `RecommenderConfig.TOP_K_RECOMENDACIONES = 3` existía
pero no se aplicaba como default real de `recomendar_hibrido()`.

**Solución**:
1. `recomendar_hibrido(top_k=None)` → si `None`, lee `settings.recomendacion.top_k`
2. `settings.recomendacion.top_k` lee `REC_TOP_K` del `.env` (default `3`)
3. El endpoint `/api/recomendar` acepta `top_k` opcional; si no viene, usa el settings
4. Eliminado el `top_k=5` hardcodeado de `obtener_recomendaciones()`

---

## Endpoints

| Método | Path                                | Descripción                        |
|--------|-------------------------------------|------------------------------------|
| GET    | `/`                                 | Frontend HTML                      |
| GET    | `/health`                           | Estado del sistema                 |
| GET    | `/api/config`                       | Config activa (sin secrets)        |
| POST   | `/api/sifods`                       | Consultas sobre la plataforma SIFODS (RAG) |
| POST   | `/api/recomendar`                   | Recomendación de cursos            |
| POST   | `/api/admin/refrescar-recomendador` | Recarga datos sin restart          |

> No hay endpoint unificado ni clasificador — cada módulo tiene su endpoint dedicado.

---

## Inicio Rápido

```bash
# 1. Copiar y configurar .env
cp .env.example .env
# Editar .env con tus credenciales reales

# 2. Instalar dependencias
pip install fastapi uvicorn openai google-genai qdrant-client \
    sentence-transformers psycopg2-binary pandas tiktoken \
    scikit-learn scipy openpyxl python-dotenv

# 3. Ejecutar
python app.py
# o: uvicorn app:app --reload --port 7002
```

