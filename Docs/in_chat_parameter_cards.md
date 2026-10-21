# In-chat parameter clarification cards

## What and why

Parameter clarification cards are rendered in the Scenario Builder chat timeline. The active card remains interactive and submits confirmation through the workflow action API. Confirmed and automatically confirmed cards are loaded from the append-only card audit and rendered as read-only, muted snapshots, so their status and values remain visible after a page reload. User confirmations also create durable timeline notes at the card's original chat position, using the same workflow event record as approval notes. When a plan is ready, the note reports that it was generated and offers a View plan button that opens the Artifact panel on Implementation Plan. The synthetic `[ACTION] card_confirmed` chat acknowledgement has been removed.

## How to run

Start the backend from the repository root:

```powershell
cd C:\Users\nidal.shahin\Code\ai-aml-agent
.\run_dev.ps1
```

Start the frontend from its repository root:

```powershell
cd C:\Users\nidal.shahin\Code\AI_AML_INTERFACE
npm run dev
```

## Configuration

No new configuration is required. Confirmed card snapshots use the existing workflow SQLite audit store.
