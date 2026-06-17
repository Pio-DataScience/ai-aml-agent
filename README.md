# AI AML Agentic Scenario Builder

> **PioTech Internal | Confidential**

A production-grade, fully autonomous multi-agent system that translates a bank compliance manager's natural language intent into live, validated AML detection scenarios — end-to-end, with no manual technical steps.

---

## What This System Does

1. **Understands** the manager's AML detection goal via conversational chat
2. **Delegates** SQL generation to the PioTech AI text-to-SQL agent (DWH/AML service)
3. **Decomposes** the generated SQL into Query Builder parameter tables
4. **Writes** the scenario to the PioTech AML Oracle Query Builder engine
5. **Self-validates** the scenario by hunting its own output — checking alert counts and sample data

---

## Architecture

See [`docs/01_architecture.md`](docs/01_architecture.md) for the full architectural proposal, node breakdown, state schema, and open questions.

---

## Project Status

> 🔴 **Pre-implementation — Architecture under review.**

---

## Repository Structure

```
AI_AML_AGENT/
├── docs/
│   └── 01_architecture.md   ← Architectural proposal (start here)
├── CHANGELOG.MD
└── README.md
```

---

## Changelog

See [`CHANGELOG.MD`](CHANGELOG.MD).
