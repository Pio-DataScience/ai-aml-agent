# AI AML Scenario Builder Agent — Service A

> Autonomous AI Agent service for natural language AML detection scenario design, DWH shadow testing, and production deployment.

---

## 🚀 Overview

The **AML Builder Agent** is a FastAPI service running an autonomous LangGraph ReAct agent. It allows compliance officers to design AML scenarios in plain English, extracts structured Pydantic contracts (`AMLIntent`), performs live Oracle DWH shadow testing via Service B (PioTech AI), and persists validated scenarios into production tables (`PIO_AML_PRODUCTION_SCENARIOS`).

- **Port**: `8005` (default)
- **API Documentation**: `http://localhost:8005/docs`
- **Primary Endpoint**: `POST /chat/stream` (Server-Sent Events)

---

## ⚙️ Configuration (`.env`)

Copy `.env.example` (or create `.env`) in the root directory:

```env
# --- Service Configuration ---
SERVICE_PORT=8005
DEBUG=false

# --- LLM Provider ---
LLM_PROVIDER=openai               # "openai" or "lmstudio"
LLM_MODEL=gpt-4o
LLM_MODEL_FAST=gpt-4o-mini
OPENAI_API_KEY=sk-proj-...
LLM_TEMPERATURE=0.0
LLM_MAX_TOKENS=16384

# For local LM Studio (only if LLM_PROVIDER=lmstudio)
# LLM_BASE_URL=http://127.0.0.1:1234/v1

# --- Oracle Database (BI_DWH) ---
ORACLE_DSN=192.168.1.100:1521/DWH_SERVICE
ORACLE_USER=your_user
ORACLE_PASSWORD=your_password
ORACLE_POOL_MIN=2
ORACLE_POOL_MAX=10

# --- Service B (PioTech AI SQL Engine) ---
PIOTECH_AI_URL=http://localhost:8001/chat/stream
PIOTECH_AI_TIMEOUT_SECONDS=120
PIOTECH_AI_USER_ID=aml_builder_agent
```

---

## 🏃 How to Run the Service

You can run the service in two ways:

### Method 1: Direct `uvicorn` Command (Standard FastAPI Way)

#### PowerShell (Windows):
```powershell
# 1. Set PYTHONPATH to the services directory
$env:PYTHONPATH = "services\aml_builder"

# 2. Run uvicorn directly
.\.venv\Scripts\python.exe -m uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
```

#### Bash (Linux / macOS / Git Bash):
```bash
# 1. Set PYTHONPATH
export PYTHONPATH="services/aml_builder"

# 2. Run uvicorn
uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
```

---

### Method 2: Using the `run_dev.ps1` Helper Script (PowerShell)

If you are on Windows, `run_dev.ps1` automatically exports `.env` variables, sets `PYTHONPATH`, creates the `artifacts/` folder if missing, and starts `uvicorn`:

```powershell
.\run_dev.ps1
```

---

## 📡 API Endpoints Overview

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service health check |
| `POST` | `/chat/stream` | SSE streaming chat endpoint for scenario building |
| `GET` | `/chat/{project}/{chat_id}/{user}/history` | Retrieves conversation history & artifacts |
| `GET` | `/chat/{project}/{user}/sessions` | Lists user chat sessions |
| `PATCH` | `/chat/session/title` | Renames a chat session |
| `DELETE` | `/chat/{project}/{chat_id}/{user}` | Deletes a chat session |

---

## 📁 Key Files Structure

```text
AI_AML_AGENT/
├── .env                              # Environment configuration
├── run_dev.ps1                       # Windows dev launcher script
├── README.md                         # This file
├── Docs/
│   └── 01_architecture.md            # Complete architecture & design documentation
└── services/
    └── aml_builder/
        └── web/
            ├── api/
            │   └── main.py           # FastAPI routes & SSE streaming
            └── services/
                ├── agent_tool_driven.py # Core ReAct agent & tools
                ├── schemas.py        # Pydantic data contracts (AMLIntent)
                ├── explanation_code_search.py # Vector embedding RAG search
                ├── oracle.py         # Oracle DB connection pool
                └── production_registry.py # Production table writer
```

---

## 📝 Documenting Code Changes

Per project collaboration protocol, whenever modifying code:
1. Update [`CHANGELOG.md`](file:///c:/Users/abura/Development/AI_AML_AGENT/CHANGELOG.md) with what changed.
2. Ensure all code passes syntax check: `python -m py_compile ...`
