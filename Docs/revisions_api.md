# Reserved Revision Restore API Contract

This document reserves the future restore operation for P6b. P3 does not expose
or implement a restore endpoint.

## Route

`POST /workflow/{project_id}/{chat_id}/{user_id}/revisions/{revision_id}/restore`

The request will carry the current `revision_id` and an optimistic-lock
`version`. The selected historical revision is the source snapshot; it does not
rewind LangGraph checkpoint state.

## Copy-forward behavior

When implemented, restore will create a new current revision whose
`restored_from_revision_id` references the selected history revision. The
displaced current revision moves to history. The restored content is copied
forward; it does not mutate the historical row. Plan and result approvals are
cleared, the conflict check runs again, and a prior shadow result is shown as a
previous run that must be re-run before approval. Normal history-cap eviction
applies, while deployed revisions and card-audit rows remain unchanged.

The operation will require the caller to own the chat, send a current
`revision_id`, and pass the workflow version check. A stale revision or version
will return a typed conflict. P3 only documents this contract; the route is
reserved and not registered.

