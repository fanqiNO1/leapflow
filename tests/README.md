# LeapFlow Test Suite

## Two-Layer Architecture

**Mock layer** (`tests/*.py`) — Fast, hermetic unit/component tests. No network, no LLM calls. Uses `StubLLM` for deterministic responses.  
Markers: `unit`, `component`.

**Real layer** (`tests/journeys/`) — 6 coarse-grained end-to-end journeys driving a real `leapd` subprocess over RPC with the LLM boundary served by a local cassette proxy.  
Markers: `e2e`, `slow`.

---

## CI Pipeline (3 Tiers)

| Tier | Workflow | Trigger | What runs |
|------|----------|---------|-----------|
| **PR Gate** | `ci.yaml` → `pr` job | Pull request | Lint → fixture consistency → real layer (journeys + regression) → **full** mock layer |
| **Main Full** | `ci.yaml` → `main` job | Push to main | Matrix (ubuntu+macos × Python 3.11/3.12/3.13), full mock + real layer |
| **Nightly Live** | `nightly-live.yaml` → `live` job | Cron, `workflow_dispatch`, or a PR labelled `ci:live` | Live-capable journeys against a real provider |
| **Re-record** | `nightly-live.yaml` → `rerecord` job | `workflow_dispatch` only | Captures real traffic into `recordings/`, opens a PR |
| **Impact map** | `nightly-live.yaml` → `impact-map` job | Cron / dispatch | Rebuilds `tests/.impact/coverage_map.json`, opens a PR |

Neither offline lane selects tests. The always-on tiers cannot be selected away
and already dominate the run (~14s of ~18s), so scoping the mock layer would save
only a few seconds — not worth any risk of under-selecting. The **live** lane does
select, because there each journey costs real tokens.

Only the two live-provider jobs need credentials — see [Credentials](#credentials).
The PR and main gates are fully offline by design.

---

## Make Targets

| Target | Purpose |
|--------|---------|
| `make test` | Default gate: mock + e2e |
| `make test-unit` | Mock layer only (`-m "not e2e"`) |
| `make test-e2e` | Real layer (journeys + regression) |
| `make test-full` | All tests, no filter |
| `make test-impact` | Change-scoped mock layer (local convenience; not used by CI) |
| `make test-live` | Real provider (requires credentials) |
| `make seed-cassettes` | Rebuild offline replay store |
| `make record-traffic` | Record real provider traffic |
| `make sync-fixtures` | Derive mock-layer response shapes from recordings |

---

## Harness (`tests/_harness/`)

| Module | Role |
|--------|------|
| `cassette_proxy.py` | Local OpenAI-compatible HTTP endpoint with 4 modes: `replay`, `seed`, `record`, `live` |
| `cassette.py` | Fingerprint computation, cassette persistence, miss diagnostics |
| `leapd.py` | Spawn real daemon subprocess with environment isolation |
| `journey.py` | Journey runner with phase attribution, deadline, call-budget and token-budget enforcement |

---

## Fixtures (`tests/_fixtures/`)

- `cassettes/` — Per-journey deterministic replay data (rebuilt by `make seed-cassettes`)
- `recordings/` — Real provider traffic evidence (captured by `make record-traffic`)
- `llm_responses/response_shapes.json` — Derived fixture asserted by mock layer (rebuilt by `make sync-fixtures`)

---

## Regression Guards (`tests/regression/`)

| File | Purpose |
|------|---------|
| `test_suite_budget.py` | Hard ceiling on journey count (merge, don't raise) |
| `test_incident_ledger.py` | Ensures past-escaped incidents stay covered |
| `test_test_layer_contracts.py` | Meta-tests preventing suite degradation |
| `test_provider_shape_drift.py` | Catches provider response format changes |
| `test_impact_selection.py` | Validates change-scope selection logic |

---

## Cost and Convergence Guards

Every journey declares two ceilings, both enforced at the cassette proxy and
reported by `journey.finish()`. Exceeding either returns HTTP 400 — non-retryable
on purpose, so a runaway loop stops at the ceiling instead of feeding the
provider's retry logic.

| Ceiling | Default | Catches |
|---------|---------|---------|
| `max_llm_calls` | `DEFAULT_MAX_LLM_CALLS = 12` | A turn that stops converging and keeps re-asking the model |
| `max_llm_tokens` | `DEFAULT_MAX_LLM_TOKENS = 150_000` | Prompt growth — a longer system prompt or bigger tool catalogue raises cost *without* adding a round, which call count cannot see |

Measured against a real provider (qwen3.7-plus), the same journey has cost between
4 and 7 provider calls on different runs: the model decides how many tool
round-trips to make. The ceilings are sized to absorb that swing, so raising one
means investigating, not editing.

---

## Live Journey Selection

Each journey declares its own metadata, read by `tools/impact.py` via AST (never
imported):

- `SUBJECT_PATHS` — the source areas the journey exercises
- `LIVE_SIGNAL` — whether running it against a real provider adds signal

`tools/impact.py --live-journeys` picks from those. A scheduled run takes every
live-capable journey; a `ci:live`-labelled PR takes only the journeys whose
subjects the change touches. `LIVE_SIGNAL = False` journeys (control plane,
lifecycle) never run live. `test_r4_recovery.py` additionally refuses to run live
at runtime, because every response it asserts on is an injected failure that a
forwarding mode cannot produce.

---

## LLM Test Modes

Controlled by `LEAPFLOW_TEST_LLM_MODE` env var:

| Mode | Reaches a provider? | Persists to | Behaviour |
|------|--------------------|-------------|-----------|
| `replay` | No | — | CI default. Deterministic playback from `cassettes/`; a miss fails with a nearest-neighbour diff |
| `seed` | **No** | `cassettes/` | Serves the journey's declared script and stores it, building the offline replay store without any credential |
| `record` | **Yes** | `recordings/` | Forwards to the real provider and stores the response as wire-shape evidence — never into the replay store |
| `live` | **Yes** | nothing | Runs against the real provider, persists nothing |

A multi-turn agent conversation **cannot** be replayed from a recording: turn *n*'s
prompt embeds the exact round-by-round history of turns 1..n-1, so one divergence
(a tool call the model made this time but not last time) cascades. That is why
`record` writes to a separate store and can never break the offline lanes.

---

## Credentials

### The offline lanes need none

`ci.yaml` (`pr` and `main`) sets `LEAPFLOW_TEST_LLM_MODE: replay` and serves the
LLM boundary from the committed cassette store. This is a requirement, not a
convenience: a pull request from a fork cannot read secrets, so anything that
gates a merge has to run without them. Never add a secret dependency to those
jobs — a credential in the PR gate silently stops fork contributions from being
mergeable.

### The live lanes need exactly three

Only `nightly-live.yaml` reaches a real provider. Both of its credential-using
jobs (`live`, `rerecord`) declare `environment: live-llm`, so configure these as
**environment** secrets under that environment — not repository secrets.

Note that a secret's name and the environment variable it feeds are two separate
namespaces, and for the model they deliberately differ:

```yaml
# nightly-live.yaml
LEAPFLOW_LLM_API_KEY: ${{ secrets.LEAPFLOW_LLM_API_KEY }}
LEAPFLOW_LLM_BASE_URL: ${{ secrets.LEAPFLOW_LLM_BASE_URL }}
LEAPFLOW_LLM_MODEL: ${{ secrets.LEAPFLOW_LLM_CHEAP_MODEL }}
#      ↑ env var the test process reads      ↑ secret name you create
```

| Create this secret | It is injected as | Example |
|--------------------|-------------------|---------|
| `LEAPFLOW_LLM_API_KEY` | `LEAPFLOW_LLM_API_KEY` | provider API key |
| `LEAPFLOW_LLM_BASE_URL` | `LEAPFLOW_LLM_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| **`LEAPFLOW_LLM_CHEAP_MODEL`** | **`LEAPFLOW_LLM_MODEL`** | `qwen3.7-plus` |

The model secret carries `CHEAP` in its name on purpose: journeys assert
invariants, not prose quality, so the lane should run on the cheapest model that
still follows tool-calling instructions. The name is the reminder.

> **Do not create a secret literally named `LEAPFLOW_LLM_MODEL`.** Nothing reads
> it — `nightly-live.yaml` only ever references `secrets.LEAPFLOW_LLM_CHEAP_MODEL`.
> A secret under the wrong name leaves the model empty, which skips rather than
> fails (see below).

Resolution path: the workflow injects `LEAPFLOW_LLM_*` → `upstream_from_env()`
reads `LEAPFLOW_TEST_UPSTREAM_*` first and falls back to `LEAPFLOW_LLM_*` → the
`journey_mode` fixture **skips** (not fails) when any of the three is empty. A
live lane that reports "skipped" is a missing or misnamed credential, not a
passing run — check the job output shows journeys actually executing.

Two details worth getting right when setting this up:

- **Create the `live-llm` environment before the first run.** If it does not
  exist, GitHub auto-creates an unprotected one on first use and the secrets have
  nowhere to live, so every journey skips.
- **Repository-level would also resolve, and that is the problem.** Environment
  secrets are readable only by jobs that enter the environment; repository
  secrets are readable by every job, which defeats the isolation.

**Leave both protection rules off.** Each one breaks a trigger this workflow
actually uses:

| Rule | Why not |
|------|---------|
| Required reviewers | A `schedule`-triggered job waits for a human instead of running. The cron would never run unattended, and each night queues another pending deployment — which defeats the drift detection the lane exists for. |
| Deployment branches restricted to `main` | On a `pull_request` event `github.ref` is `refs/pull/N/merge`, which does not match `main`, so a `ci:live` run fails with *"Branch is not allowed to deploy to live-llm"*. |

The protection comes from three properties that hold without either rule:

1. **Fork pull requests never receive secrets.** GitHub does not pass them for
   `pull_request` events from forks, so a fork cannot spend tokens even with the
   label applied — the journeys skip instead.
2. **The `ci:live` label requires write or triage permission.** Applying it is the
   gate for same-repo pull requests, and anyone able to apply it can already run
   `workflow_dispatch`.
3. **Every journey caps its own provider calls and tokens.** Worst-case spend is
   bounded by construction, not by trust — see [Cost and Convergence
   Guards](#cost-and-convergence-guards).

### Setting it up in the GitHub UI

**1. Create the environment** — *Settings → Environments → New environment*, named
`live-llm`. Leave **Required reviewers** unchecked and **Deployment branches** at
*All branches*, for the reasons in the table above.

**2. Add the three secrets** — on that environment's page, under **Environment
secrets** (not the repository secrets above it), add each row of the table above.
The third one's name is the easy mistake: it is `LEAPFLOW_LLM_CHEAP_MODEL`.

**3. Verify** — *Actions → Nightly live → Run workflow* (leave `rerecord` false).

| Result | Meaning |
|--------|---------|
| `3 passed` | Configured correctly |
| `3 skipped` | A secret is missing or misnamed — the log names which: `missing ['LEAPFLOW_LLM_MODEL']` |

Expanding the *Decide which journeys to run* step shows the selected journeys.

**Optional — on-demand live runs per pull request.** Create a label named
`ci:live` (*Issues → Labels → New label*). Applying it to a PR runs only the live
journeys that PR's change could affect. Applying a label needs write or triage
permission, which is the gate for same-repo pull requests.

Measured cost of a full scheduled run against `qwen3.7-plus`: 15–20 provider
calls, ~135k tokens, ~55s. The structural worst case, fixed by the per-journey
ceilings, is 36 calls / ~342k tokens.

`LEAPFLOW_LLM_CHEAP_MODEL` holds a model name rather than a credential, but the
workflow reads it through `secrets.`, so it has to be stored as a secret to take
effect. Moving it to `vars.` would make it visible and diff-reviewable — and would
remove the name mismatch as a source of confusion — at the cost of editing both
call sites (`nightly-live.yaml` L91 and L137).

### What deliberately does not need configuring

`leapd.py` strips **every** inherited `LEAPFLOW_*` from the daemon environment
and injects a fixed set, so extra variables set in CI never reach a journey's
daemon — they only affect the three fallbacks above.

| Variable | Why it is not a secret |
|----------|------------------------|
| `LEAPFLOW_VLM_*`, `LEAPFLOW_LLM_AUX_*` | Pointed at the same cassette proxy on purpose, so no journey can reach a real provider through a side channel |
| `LEAPFLOW_MOCK_HOST` | Harness sets `1` |
| `LEAPFLOW_DATA_DIR`, `LEAPFLOW_PROFILE`, `LEAPFLOW_LLM_MAX_RETRIES`, `LEAPFLOW_LLM_CONTEXT_LENGTH`, `LEAPFLOW_LOG_LEVEL`, `LEAPFLOW_DAEMON_*` | Harness-controlled per journey |
| `LEAPFLOW_TEST_UPSTREAM_BASE_URL`, `_API_KEY`, `_MODEL` | Optional override, for pointing the recorder at a different endpoint than the one under test |
| `LEAPFLOW_TEST_LLM_MODE` | Set explicitly per job, not a secret |

### Running the live lane locally

```bash
export LEAPFLOW_LLM_API_KEY=...
export LEAPFLOW_LLM_BASE_URL=https://.../v1
export LEAPFLOW_LLM_MODEL=qwen3.7-plus
make test-live      # or: make record-traffic
```

---

## Mock Signal Injection (`tests/mock_signals/`)

Standalone framework for injecting simulated real-time streaming signals into the full LeapFlow pipeline. Exercises: EventBus → normalize → memory → EventBridge → EventTrigger → MonitorManager → Finding.

### How it works

```
SignalGenerator(config) → EventBus.handle_event(event_type, payload)
  → normalize → privacy gate → memory ingest → _notify_subscribers()
    → EventBridge.on_event() → trigger.matches() → debounce → trigger.notify()
      → MonitorManager.run_watch_once() → Producer.observe() → Finding
```

The runner builds an in-process pipeline (pure-memory providers + temp DuckDB), arms event-driven watches for each signal type, then injects events concurrently via `asyncio.gather`.

### Usage

```bash
python -m tests.mock_signals                     # Default "normal" profile
python -m tests.mock_signals -p burst            # High-frequency burst
python -m tests.mock_signals -p stress           # Max throughput stress test
python -m tests.mock_signals -p mixed            # All signal types simultaneously
python -m tests.mock_signals -p gateway          # External platform signals
python -m tests.mock_signals --list              # Show all profiles
python -m tests.mock_signals -p normal -d 30     # Override duration (seconds)
python -m tests.mock_signals -p burst -f 2.0     # Frequency multiplier
```

### Signal types

| Generator | Event type | Configurable parameters |
|-----------|------------|------------------------|
| `FsChangeGenerator` | `fs.change` | paths, actions (created/modified/deleted/moved) |
| `AppFocusGenerator` | `app.focus_change` | apps (bundle_id + app_name) |
| `ClipboardGenerator` | `clipboard.change` | texts, content_type |
| `InputGenerator` | `ui.action` | action_type (click/type/scroll), app_bundle_id |
| `GatewaySignalGenerator` | `gateway.signal` | platform_id, signal_type |
| `GatewayMessageGenerator` | `gateway.message.received` | platform_id, sender |

### SignalConfig (common to all generators)

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `frequency_hz` | 1.0 | Events per second |
| `burst_size` | 1 | Events per burst |
| `burst_interval_s` | 0.0 | Pause between bursts |
| `duration_s` | 5.0 | Total generation time |
| `jitter_ms` | 50.0 | Random timing jitter |

### File structure

```
tests/mock_signals/
├── __init__.py       # Public API exports
├── __main__.py       # CLI entry point
├── generators.py     # 6 signal generators (BaseGenerator + subclasses)
├── profiles.py       # 5 predefined scenarios (normal/burst/mixed/stress/gateway)
└── runner.py         # Orchestrator: pipeline setup → inject → measure → report
```

---

## Quick Start

```bash
# Default test gate (what CI runs)
make test

# Mock layer only (fast, ~18s)
make test-unit

# Real journeys only (~7s, offline)
make test-e2e

# Change-scoped mock layer (local convenience during development)
make test-impact BASE=origin/main

# Rebuild the offline replay store after changing journey logic
make seed-cassettes
make sync-fixtures
```
