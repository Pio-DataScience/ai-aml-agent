# AI AML Agentic Scenario Builder

> **PioTech Internal | Confidential**

A production-grade, fully autonomous multi-agent system that translates a bank compliance manager's natural language intent into live, validated AML detection scenarios — end-to-end, with no manual technical steps.

---

## What This System Does

1. **Understands** the manager's AML detection goal via conversational chat (FastAPI `/chat/stream` SSE endpoint).
2. **Delegates** SQL generation to the PioTech AI text-to-SQL agent (DWH/AML service).
3. **Decomposes** the generated SQL into Query Builder parameter tables.
4. **Writes** the scenario to the PioTech AML Oracle Query Builder engine.
5. **Self-validates** the scenario by hunting its own output — checking alert counts and sample data, with automatic retry/healing loops.

---

## Architecture

See [01_architecture.md](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/01_architecture.md) for the full architectural proposal, node breakdown, state schema, and open questions.
See [02_architecture.md](file:///c:/Users/abura/Development/AI_AML_AGENT/Docs/02_architecture.md) for the chat interface integrations.

---

## Project Status

- **Phase 1-4 Implementation**: Complete! The LangGraph multi-agent system is fully implemented and compiled.
- **Oracle Integration**: Verified and connected via `oracledb`.
- **Checkpoint Persistence**: Fixed with SQLite checkpointer (`SqliteSaver`).

---

## Installation & Setup

### 1. Prerequisites
- Python 3.10+
- Access to an Oracle Database instance (defined in `.env`)
- OpenAI API Key (or alternative configured LLM provider)

### 2. Install Dependencies
Create a virtual environment and install the required packages:

```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r services/aml_builder/requirements.txt
```

### 3. Environment Configuration
Copy `.env.example` to `.env` in the root of the project and configure the required settings:

```bash
cp .env.example .env
```

Open `.env` and fill in:
- `ORACLE_USER`
- `ORACLE_PASSWORD`
- `ORACLE_DSN`
- `OPENAI_API_KEY`

---

## Running the Service

### On Windows (PowerShell)
You can run the pre-configured script from the repository root:
```powershell
.\run_dev.ps1
```

### Manual Run (All Platforms)
If you prefer running manual commands, make sure the python path is configured so that imports resolve correctly:

**Windows (PowerShell):**
```powershell
$env:PYTHONPATH = "services/aml_builder"
uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
```

**Linux / macOS / Git Bash:**
```bash
export PYTHONPATH=services/aml_builder
uvicorn web.api.main:app --reload --port 8005 --host 0.0.0.0
```

---

## Verifying & Testing the API

Once the service is running, you can access the interactive API docs at [http://localhost:8005/docs](http://localhost:8005/docs).

### Test via Postman or Curl

Send a `POST` request to `http://localhost:8005/chat/stream`:

```bash
curl -N -X POST http://localhost:8005/chat/stream \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Flag customers doing more than 5 cash transactions above JD 5000 in 30 days"}],
    "metadata": {"user_id": "test_user", "chat_id": "test_001", "project_id": "0"},
    "reasoning_mode": "instant"
  }'
```
*(Use SSE client or set Response Type to Text in Postman to watch the stream of agent execution steps).*
