# Tempo Test Harness Design

## Goal

Pin all core pipeline behavior so the codebase can be aggressively simplified without breaking functionality. Record/replay LLM responses as fixtures; tests run fast and deterministic without API keys.

## Architecture

### Record/Replay Provider

`RecordReplayProvider(ModelProvider)` wraps `chat_completion()`:

- **Record mode**: forwards to real provider, saves `{sha256(prompt): response}` to fixture JSON
- **Replay mode**: looks up prompt hash, returns cached response. Raises `FixtureMissing` if not found.

Hash key = SHA256 of the user message content string (stable across refactors that don't change prompt text).

Fixture files: `tests/fixtures/{name}.json` — list of `{prompt_hash, prompt_preview, response}` objects.

### Test Infrastructure (conftest.py)

- **In-memory SQLite** via temp directory + `Database` class
- **Replay provider** loaded from committed fixtures
- **Store** wired to test DB session
- **Sample data factories**: helpers to create operations/actions with timestamps

### Record Script

`tests/record_fixtures.py` — run once with Vertex AI credentials:
1. Creates real Gemini provider
2. Feeds sample observation text through each pipeline stage
3. Captures `(prompt_hash, response)` pairs
4. Writes to `tests/fixtures/*.json`

## Test Coverage

### test_stage1_operations.py
- `ObservationAdapter.process_observation()` creates Entity rows with type="operation"
- Correct timestamps, metadata (confidence, decay, screenshot_path)
- Malformed JSON fallback produces single fallback operation

### test_stage2_actions.py
- `ActionBuilder.build_actions()` creates actions from buffered operations
- Structural relations: operation → action via PART_OF
- Temporal relations: FOLLOWS between sequential actions, CO_OCCURS within batch
- Cross-batch chaining to most recent existing action

### test_stage3_activities.py
- `ActivityProposeJob.run()` creates activity entities from actions
- Behavioral relations: SUPPORTS/HINDERS with valence
- Action-to-activity structural linking

### test_stage4_goals.py
- `GoalSynthesisJob.run()` creates goal entities from activities
- Goal-activity structural relations
- `GoalProposeJob` + `GoalReconcileJob` integration

### test_buffer.py (no LLM — pure logic)
- Size trigger fires at max_buffer_size
- Time trigger fires after inactivity threshold
- Failed callback preserves operations in buffer
- Trigger suppression after failure (60s cooldown)
- State round-trip: serialize → deserialize preserves operations

### test_orchestrator.py
- single production-pipeline start/stop/resume lifecycle
- State save/restore round-trip
- Sleep/wake gap detection resets timers

### test_resource_lifecycle.py
- timer tasks are cancelled and awaited on close
- observer restart does not duplicate a live worker
- providers, observer, and database clients close exactly once

### test_server_contract.py
- exact retained HTTP/WebSocket route surface
- hostile Host and Origin rejection, including WebSocket origins
- loopback/Electron CORS behavior
- API-key redaction and owner-only persisted configuration
- screenshot paths cannot escape the screenshot directory

### test_frontend_surface.py
- the production shell exposes exactly Record, Hierarchy, and Search
- scheduled legacy/research views and graph dependencies stay deleted
- core UI code is browser-safe and runtime-validates API responses
- the typed client matches retained routes and owns WebSocket cleanup
- the hierarchy editor covers every transactional mutation, revision conflicts, legal reparenting, and drag lifecycle
- both frontend targets remain genuinely strict with no explicit `any`/suppression escape hatches, while dialogs, hierarchy semantics, focus visibility, reduced motion, and narrow layouts retain accessibility guards

### test_hierarchy_api.py
- full depth-limited hierarchy serialization
- create, update/lock, reparent, merge, split, and delete/reparent behavior
- legal tier, exact partition, cycle, locked-node, and stale-revision validation
- pipeline writes advance revisions and reconcile cannot merge away user edits
- injected mid-mutation failures roll back and WebSocket events follow commit
- database managers for one SQLite file share a weakly held write lock

### test_exclusions.py
- macOS application-root discovery, bundle-ID deduplication, and icon caching
- Linux `.desktop` discovery and hidden-entry filtering
- normalized, atomic, owner-only exclusion settings
- bundle-ID and browser-domain boundary matching
- observer-owned polling cadence and removal of the Electron pause/resume loop

### test_public_api.py
- synchronous `tempo.Tempo` goals, hierarchy, search, timeline, and trace results
- public dataclass records do not expose SQLAlchemy models or live sessions
- notebook-style active event loops use one owned worker and close it after each query

### test_cli_privacy.py
- `tempo data-dir` resolves the configured local data root exactly
- screenshot-only purge preserves the database
- full purge removes contents while preserving the explicitly selected root

### Phase 7 packaging guards
- the Python package declares generated `tempo.ui` data and discovers `tempo.*` packages
- one shared frozen/typechecked UI build feeds both wheel and Electron-server packaging
- FastAPI serves hashed assets and SPA routes with restrictive security headers
- browser mode selects the serving origin without depending on Electron APIs
- test diagnostics remain isolated from the user's real Tempo data directory

## File Structure

```
tests/
  conftest.py
  fixtures/
    stage1_obs_to_ops.json
    stage2_ops_to_actions.json
    stage3_actions_to_activities.json
    stage4_goal_synthesis.json
  record_fixtures.py
  test_stage1_operations.py
  test_stage2_actions.py
  test_stage3_activities.py
  test_stage4_goals.py
  test_buffer.py
  test_orchestrator.py
  test_resource_lifecycle.py
  test_server_contract.py
  test_frontend_surface.py
  test_hierarchy_api.py
  test_exclusions.py
  test_public_api.py
  test_cli_privacy.py
```
