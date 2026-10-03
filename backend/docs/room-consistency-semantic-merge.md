# Room consistency semantic merge manifest

Canonical issues: #76 (F01/F05), #86 (Game lifecycle), #88 (Realtime lifecycle).

This document is a **merge-time invariant list**, not a claim that the combined branch has already
passed pinned repository, Redis, 2-Pod, Browser, CI or deployment gates.

## Intended promotion order

```
#82 -> #86 -> #88 -> F05/F01 (#76) -> #104 -> #85
```

The three stability branches are siblings under #82 today. Do not resolve conflicts by choosing one
whole file. Merge the following semantics into the final files after each predecessor is on main.

## Overlap inventory

Compared with `82_ux/board-room-polish-v2`:

- #86 ↔ #88: 8 files.
- #86 ↔ F05/F01: 5 files.
- #88 ↔ F05/F01: 3 files.

High-risk files are `app.py`, `room_scripts.py`, Memory Room adapter and the shared Room contract
tests.

## app.py final invariants

Start from the merged #86 lifecycle composition, then preserve F05 production admission wiring.

Required final behavior:

- #86: `build_memory_game_lifecycle` / `build_redis_game_lifecycle` remain wired.
- #86: `captured_startup`, invalidation and normal-completion dependencies remain connected.
- F05: production `RedisSessionWorkflow` receives the guarded Room admission authority:
  `RedisSessionWorkflow(providers.sessions, room_service, providers.rooms)`.
- F05: headless Memory Room runtime uses the guarded Memory adapter without creating another
  participation authority.
- #88: Realtime/connection coordinator wiring remains unchanged except for its finalized lifecycle
  semantics.
- Do not create a second Redis client or another background loop.

## room_scripts.py final invariants

The final `ROOM_MUTATION` must contain all of the following. A higher script version than every
parent branch is required after semantic merge.

### From #86

- Captured lifecycle-compatible Room transitions and its final participant/complete-game guards.
- Any script/schema/version changes already required by the frozen Game lifecycle branch.

### From #88

- Disconnect records `connected=false` and a **10-second** expiry without immediate owner handoff.
- Expiry performs the confirmed departure/handoff/reset exactly once.
- Start rejects a disconnected owner.
- Only `ready && connected` participants count toward minimum Ready, both-team eligibility and the
  PLAYER roster.

### From F01

- Join does **not** use the Lobby-observed aggregate Room `state_version` as an exact mutation
  precondition.
- Join still checks current Room existence/lifecycle, capacity, password authorization and duplicate
  participant state at the server write.

### From F05

- The admission-fence script wraps the **finally merged** `ROOM_MUTATION.source`; do not freeze a
  pre-#86/#88 copy of the Room script.
- Create/join/change_identity/connect require the per-Session token fence and live Session check
  before the first inherited Room-script write.
- The token stays outside the original request payload/fingerprint.
- Existing ambiguous-binding detection stays fail-closed.

## Memory Room adapter final invariants

- Preserve #86 captured lifecycle/start-intent support.
- Preserve F01 Join current-state semantics: no exact aggregate version gate for Join.
- The guarded `SessionAdmissionMemoryRoomAdapter.join()` must not reintroduce
  `_require_expected_version(...)` before delegating to the base current-state Join.
- Preserve F05 shared-store Session admission check immediately before create/join/identity/connect
  binding writes.
- Preserve #88 temporary-disconnect semantics and 10-second lease through the shared runtime
  constant.

## api/problems.py final invariants

- #86 internal recovery/history/proof failures remain 503, not request-validation 422.
- #88 disconnected owner Start conflict remains a 409 Room state conflict.
- Existing authorization/not-found/validation mappings remain unchanged unless a direct regression
  requires it.

## Session workflow final invariants

- Production logout holds the same F05 per-Session admission lease across Room leave and Session
  revoke, preventing admission in the leave→revoke window.
- Guest→Member rotation keeps existing rollback/uncertainty behavior. The replacement raw token is
  not exposed before the Room identity transition returns.
- Connect/identity replacement uses the guarded Room adapter and must remain compatible with #88
  generation/late-disconnect protection.

## Tests that must survive the semantic merge

Do not discard one branch's tests simply because the production file conflicts.

- #86: captured lifecycle, normal completion, startup recovery, production shell and problem-status
  mapping tests.
- #88: reconnect lease, generation replacement, disconnected Ready/owner Start eligibility,
  WebSocket/stream access and Help tests.
- F05/F01: concurrent cross-Room admission, logout/admission serialization, Join current-state
  semantics and admission-fence tests.
- Existing same-Room optimistic-concurrency tests for non-Join mutations.

## Merge-time checks before any PR promotion

1. Compare the merged file to all three parent heads; verify every invariant above.
2. Search for stale values/semantics:
   - `ROOM_DISCONNECT_LEASE_MS = 30 * 1000`
   - Join exact-version guard inside the Join operation.
   - unguarded production `RedisRoomRuntimeAdapter` construction.
   - two-argument production `RedisSessionWorkflow(providers.sessions, room_service)`.
   - guarded Memory Join calling `_require_expected_version` before the base Join.
3. Confirm the admission wrapper references the final imported `ROOM_MUTATION.source`.
4. Bump the final Room mutation script version after semantic merge.
5. Run targeted shared Room contract tests before the broader pinned suite.
6. Only then run Provider/2-Pod/Browser gates when the server PC is available.

This manifest is intentionally narrow. It does not merge branches, deploy code or replace required
Git/CI/Provider evidence.
