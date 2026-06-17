# AI AML Agentic Scenario Builder — Interface Architecture
**PioTech Internal | Confidential | v1.0**
**Date: 2026-06-17**

---

## The Approach — Should We Build or Adopt?

The short answer is: **adopt + thin adapter layer**. Do not build a chat UI from scratch.

Building a production-grade chat interface (streaming rendering, conversation history, markdown/code blocks, typing indicators, error states, mobile-responsive layout) is a 3–6 month project on its own. The open-source community has already solved this problem at a level that rivals ChatGPT itself. Our time should be spent on what only *we* can build — the agent intelligence and QB integration — not on re-implementing a scroll container with streaming text.

The right strategy:

```
┌────────────────────────────────────────────────────────────────┐
│           Open-Source Chat Frontend (adopted, not built)        │
│              e.g., LibreChat — self-hosted, Docker              │
└──────────────────────────────┬─────────────────────────────────┘
                               │ HTTP / SSE
                               ▼
┌────────────────────────────────────────────────────────────────┐
│              OpenAI-Compatible Adapter Layer (thin)             │
│         Translates our custom SSE → standard OpenAI format      │
└──────────────────────────────┬─────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────┐
│              AML Scenario Agent API (our system)                │
│              FastAPI + SSE, Port 8005                           │
└────────────────────────────────────────────────────────────────┘
```

---

## The Candidates — Market Landscape

Four serious contenders were evaluated. All are open-source, self-hostable, and production-ready.

| Platform | Stars (2026) | License | Primary Strength |
|----------|-------------|---------|-----------------|
| **LibreChat** | 22k+ | MIT | ChatGPT-identical UX, enterprise auth, multi-provider |
| **Open WebUI** | 45k+ | MIT | RAG pipelines, Ollama native, Python middleware |
| **LobeChat** | 35k+ | MIT | Best visual design, agent presets, plugin marketplace |
| **NextChat** | 75k+ | MIT | Minimal, ultra-fast, zero-config |

---

## The Recommendation — LibreChat

**LibreChat** is the correct choice for this system. Here is the reasoning:

### Why LibreChat Wins for Our Context

**1. It is the closest to ChatGPT's UX — zero manager retraining needed.**
Bank compliance managers are not developers. They interact with ChatGPT daily. LibreChat's interface is intentionally identical — same message threading, same streaming text animation, same code block rendering. Adoption friction is near zero.

**2. Enterprise authentication is built in.**
LibreChat ships with LDAP, OIDC, OAuth2, and SAML support out of the box. This means it can plug directly into the bank's Active Directory or SSO provider without custom auth development.

**3. Custom endpoints via `librechat.yaml`.**
LibreChat has a first-class YAML configuration system (`librechat.yaml`) that lets us define custom API endpoints with:
- Custom base URL (our FastAPI service)
- Parameter overrides (to match our API contract)
- Per-endpoint model lists
- Stream toggle per model

**4. Multi-agent routing in one interface.**
As our platform grows (AML reporting agent + AML scenario builder agent), LibreChat can expose both as separate "models" in the same interface. The manager picks the right agent from a dropdown — no separate UIs to maintain.

**5. Self-hosted, zero data leaves the bank.**
Everything runs on-premises inside Docker. No data touches third-party servers.

---

## The One Technical Challenge — SSE Format Mismatch

This is the most important architectural insight in this document.

LibreChat expects the **OpenAI streaming SSE format**:

```
data: {"id":"...", "choices":[{"delta":{"content":"Hello "}}]}
data: {"id":"...", "choices":[{"delta":{"content":"world"}}]}
data: [DONE]
```

Our existing PioTech AI agents emit a **custom SSE format**:

```
data: {"type": "tool_call", "tool": "data_agent_tool"}
data: {"type": "thinking", "status": "executing"}
data: {"type": "content", "text": "The total revenue is..."}
data: {"type": "final_answer", "text": "Complete answer here"}
data: {"type": "done"}
```

These are **not compatible**. LibreChat will ignore or silently fail on our custom event types.

### The Solution — An Adapter Microservice

We build a **thin, stateless adapter** that sits between LibreChat and our agent API. It translates the wire format in real-time.

```python
# adapter/main.py — FastAPI streaming proxy

@app.post("/v1/chat/completions")   # ← LibreChat calls this (OpenAI format)
async def chat_completions(request: OpenAIChatRequest):
    """
    Receives OpenAI-format request from LibreChat.
    Translates to our agent's format.
    Streams response back as OpenAI-compatible SSE.
    """
    async def stream_generator():
        # 1. Forward to our AML agent API
        async for event in call_aml_agent(request):
            
            # 2. Map our custom events to OpenAI delta format
            if event["type"] == "content":
                yield openai_delta_chunk(event["text"])
            
            elif event["type"] == "tool_call":
                # Render tool activity as a styled prefix
                yield openai_delta_chunk(f"\n🔧 *{event['tool']}...*\n")
            
            elif event["type"] == "thinking":
                # Optionally surface thinking state
                yield openai_delta_chunk(f"\n⚙️ *Analyzing...*\n")
            
            elif event["type"] == "final_answer":
                yield openai_delta_chunk(event["text"])
            
            elif event["type"] == "done":
                yield "data: [DONE]\n\n"
    
    return StreamingResponse(stream_generator(), media_type="text/event-stream")
```

**This adapter is ~100–150 lines of Python.** It is not a significant engineering effort. It is a translation layer, not a system.

---

## Full Deployment Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Docker Compose Network                        │
│                                                                      │
│  ┌──────────────────┐      ┌─────────────────────────────────────┐  │
│  │   LibreChat       │      │         Adapter Service             │  │
│  │   (Frontend)      │─────►│  FastAPI — Port 8006               │  │
│  │   Port 3000       │ SSE  │  /v1/chat/completions               │  │
│  │   (self-hosted)   │      │  Translates OpenAI ↔ PioTech SSE   │  │
│  └──────────────────┘      └────────────────┬────────────────────┘  │
│                                             │                        │
│                                             ▼                        │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                  AML Scenario Agent API                       │   │
│  │                  FastAPI — Port 8005                          │   │
│  │                  POST /chat/stream                            │   │
│  └────────────────────────────┬─────────────────────────────────┘   │
│                               │                                      │
│                ┌──────────────▼───────────┐                         │
│                │   PioTech AI (DWH/AML)   │                         │
│                │   text-to-SQL Agent      │                         │
│                │   Port 8001 / 8002       │                         │
│                └──────────────────────────┘                         │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Shared Infra: Redis | Neo4j | ChromaDB | SQLite             │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## LibreChat Configuration for Our System

The `librechat.yaml` config to point LibreChat at our adapter:

```yaml
# librechat.yaml

endpoints:
  custom:
    - name: "AML Scenario Builder"
      apiKey: "${AML_AGENT_API_KEY}"
      baseURL: "http://adapter:8006"
      models:
        default: ["aml-scenario-builder-v1"]
        fetch: false
      titleConvo: true
      titleModel: "aml-scenario-builder-v1"
      summarize: false
      dropParams:
        - "frequency_penalty"
        - "presence_penalty"
        - "top_p"
        - "user"
      addParams:
        stream: true

    - name: "AML Reporting Agent"
      apiKey: "${AML_REPORT_API_KEY}"
      baseURL: "http://aml-agent:8002"    # If AML reporting already has OpenAI-compat layer
      models:
        default: ["aml-reporting-v1"]
        fetch: false
```

---

## AML-Specific UI Enhancements

LibreChat's base interface handles 90% of what we need. The remaining 10% are AML-specific requirements that we surface through **structured Markdown in the agent's response** — no frontend code changes required.

### Progress Display — In-Message Markdown

Instead of custom UI components, the agent streams structured Markdown that LibreChat renders natively:

```markdown
## 🚀 Building your AML Scenario...

| Step | Status | Detail |
|------|--------|--------|
| Intent Analysis | ✅ Done | Cash velocity, 30-day window |
| SQL Generation | ✅ Done | PioTech AI called successfully |
| QB Decomposition | 🔄 In Progress | Mapping 3 conditions... |
| QB Write | ⏳ Pending | — |
| Validation | ⏳ Pending | — |
```

### Scenario Validation Result — In-Message Card

```markdown
## ✅ Scenario Created Successfully

**Scenario ID:** `SCN-20260617-004`
**Name:** Cash Velocity — 30 Day Window

### 📊 Live Impact Assessment
- **Active Alerts:** 23 customers
- **Alert Rate:** 0.4% of total customer base (healthy range)

### 👥 Sample Matched Customers
| Customer | Transaction Count | Total Amount |
|----------|------------------|--------------|
| CUS-001  | 7                | JD 67,400    |
| CUS-045  | 6                | JD 52,100    |
| CUS-112  | 5                | JD 48,750    |

> ⚠️ Would you like to tighten the threshold, or is this volume acceptable?
```

LibreChat renders tables, badges, and code blocks out of the box. **Zero custom frontend development needed for these views.**

---

## What We Do NOT Need to Build

| Concern | Solution |
|---------|---------|
| Chat layout (bubbles, threading) | LibreChat — included |
| Streaming text animation | LibreChat — included |
| Conversation history & persistence | LibreChat — included |
| Markdown / code block rendering | LibreChat — included |
| Multi-user support | LibreChat — included |
| Authentication (LDAP/SSO) | LibreChat — included |
| Mobile-responsive layout | LibreChat — included |
| Dark mode | LibreChat — included |
| File upload (for future multimodal) | LibreChat — included |
| Custom branding (logo, colors) | LibreChat `librechat.yaml` — 5 min config |

---

## What We DO Build

| Concern | What We Build | Effort |
|---------|--------------|--------|
| **Adapter Service** | FastAPI SSE translator (`adapter/main.py`) | ~1 day |
| **Agent API** | The AML Scenario Agent (Port 8005) | Core work |
| **`librechat.yaml`** | Endpoint configuration | ~2 hours |
| **Docker Compose** | Add LibreChat + adapter to network | ~2 hours |
| **Structured Markdown** | Scenario progress & result cards in agent responses | Ongoing |

---

## Alternative Considered — LobeChat

LobeChat has a more visually impressive default design and a plugin/agent marketplace. However:
- Its "OpenAI-compatible endpoint" configuration is less mature than LibreChat's
- Its enterprise auth (SSO/LDAP) requires more custom work
- Its markdown rendering for tables (critical for us) has had known rendering bugs in some versions

For a banking compliance tool where **reliability and familiarity beat aesthetics**, LibreChat is the better call.

---

## Alternative Considered — Build Custom

Building a custom React/Next.js interface was considered and rejected for the following reasons:

- **Time cost:** 6–10 weeks minimum for a production-quality chat UI
- **Maintenance burden:** Every feature LibreChat ships (streaming, history, responsive design) becomes our responsibility to maintain
- **Risk:** The UX quality of even a well-built custom interface will be below LibreChat's maturity level for months

The only scenario where a custom build makes sense is if we need **deep integration with the PioTech AML module's own Angular frontend**. In that case, the adapter layer still applies — we would expose our agent API through the same adapter and let the Angular team embed the chat widget.

---

## Proposed Repository Structure — Interface Module

```
AI_AML_AGENT/
├── docs/
│   ├── 01_architecture.md        ← Agent system architecture
│   └── 02_architecture.md        ← This file: interface architecture
├── interface/
│   ├── adapter/                  ← The SSE adapter microservice
│   │   ├── main.py               ← FastAPI translation layer
│   │   ├── settings.py           ← Config (agent URL, API key)
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   └── librechat/
│       ├── librechat.yaml        ← LibreChat endpoint configuration
│       ├── docker-compose.yml    ← LibreChat + adapter compose file
│       └── .env.example          ← Required environment variables
└── ...
```

---

## Phased Delivery — Interface

### Phase 1 — Wire Up LibreChat (Day 1–2)
- [ ] Clone LibreChat, stand it up locally via Docker
- [ ] Write `librechat.yaml` pointing at adapter (placeholder initially)
- [ ] Verify LibreChat renders conversations correctly

### Phase 2 — Adapter Service (Day 2–3)
- [ ] Build `adapter/main.py` — SSE translation layer
- [ ] Test end-to-end: LibreChat → Adapter → AML Agent → LibreChat renders
- [ ] Handle all our custom event types (`tool_call`, `thinking`, `content`, `final_answer`, `done`)

### Phase 3 — AML Scenario Progress Cards (Ongoing)
- [ ] Design Markdown templates for each lifecycle stage
- [ ] Test how LibreChat renders progress tables, alert result cards
- [ ] Iterate based on manager feedback

### Phase 4 — Production Hardening (Final)
- [ ] Connect LibreChat auth to bank's LDAP/SSO
- [ ] Custom branding (PioTech logo, colors via `librechat.yaml`)
- [ ] Add to main `docker-compose.yml` in production

---

*Document status: **UNDER REVIEW** — awaiting critique before development begins.*
