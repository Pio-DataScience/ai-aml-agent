# Workflow approval timeline notes

Plan approvals, shadow result approvals, and user parameter confirmations are persisted in `workflow_events` by their typed workflow endpoints. The workflow state response exposes them as `approval_notes` with a revision number, timestamp, and the chat message position captured when the action occurred; it does not include approval hashes or raw event details. The UI inserts each note at that position, keeping it in place as later chat messages arrive and after history reload. The chat endpoint rejects messages flagged as workflow actions, so manually typing an `[ACTION]` string cannot be forwarded as a trusted action.

The Scenario Builder UI renders each approval as a centered timeline note in the chat feed. These notes are read back from workflow state when a chat opens, so they remain visible after navigation and reload. After approval, the UI also sends a verified workflow event to the model with plain-language continuation instructions, without adding an `[ACTION] ...` bubble to the visible transcript. The backend checks that the referenced revision is still current and approved, stores the internal handoff in the checkpoint, and hides that marked handoff when reconstructing chat history.

## Run

Start the AML workflow service using the repository development launcher:

```powershell
.\run_dev.ps1
```

Start the UI in `AI_AML_INTERFACE`:

```powershell
npm run dev
```

Open a Scenario Builder chat, create a plan, and approve it. The chat should show a centered “Revision N plan approved” note. Reopen the chat and verify the same note loads from the workflow state endpoint.

## Configuration

No new configuration is required. Approval notes use the existing workflow SQLite persistence and authenticated workflow state endpoint.
