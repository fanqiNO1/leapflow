# Cordis Spatiotemporal Composability: Design Insights & LeapFlow Upgrade Analysis

> **Date**: 2026-08-20  
> **Scope**: Comprehensive comparison of the Cordis spatiotemporal composability paper's formal model against LeapFlow's current plugin/lifecycle implementation, identifying actionable upgrade opportunities.  
> **Paper**: "A Programming Paradigm for Spatiotemporal Composability" — Yifan Shi, Wei Zhang, Tianyi Cui (Peking University, DeepSeek-AI)  
> **LeapFlow baseline**: `leapflow.plugins` first-class module, PluginFiber 6-state machine (PENDING/LOADING/ACTIVE/FAILED/UNLOADING/DISPOSED), EffectScope LIFO cleanup with async support, dependency-driven ScopedToolRegistry, scope-bound EventBus, waterfall ToolExecutionPipeline

---

## 1. Executive Summary

> **Status: P0+P1+P2 ALL IMPLEMENTED** (commits 68df756, 6e77a83, pending). Delivered capabilities: scope-bound EventBus subscriptions, 6-state PluginFiber (LOADING/FAILED + retry), dependency-driven fiber activation (fixpoint loop), topological `bind_runtime` ordering (`graphlib.TopologicalSorter`), waterfall tool execution pipeline (`ToolInterceptor` Protocol), and async EffectScope cleanup (`async_effect` + `async_dispose`). All upgrades are cold-path only with zero per-turn hot-path impact.

### Core Paper Insight

The Cordis paper establishes that **dynamic composition has two orthogonal formal dimensions**: temporal composability (every environmental modification carries a trackable inverse, enabling complete reversal on unload) and spatial composability (every dependency is declared as a specification, and the runtime reactively drives lifecycle transitions when dependencies appear/disappear). These two dimensions are unified through a single context type that carries both effects and coeffects, enabling formal guarantees: preservation, recovery exactness, progress (no deadlock), and confluence (load-order independence).

### LeapFlow's Current Position

LeapFlow already embodies the temporal dimension **partially** through EffectScope (LIFO cleanup with colocated effect registration) and PluginFiber (4-state lifecycle). However, it lacks the spatial dimension almost entirely: dependencies are manually injected via `bind_runtime()`, there is no reactive activation/deactivation on dependency availability, no hierarchical scope tree, and no service resolution protocol. The event model (flat pub/sub EventBus) cannot express Cordis's waterfall composition or scope-filtered dispatch.

### Top 5 Upgrade Recommendations

| # | Recommendation | Value | Effort |
|---|---------------|-------|--------|
| 1 | **Dependency-Driven Fiber Activation** — fibers stay PENDING until all declared deps are available; become UNLOADING when deps disappear | Eliminates manual activation orchestration, prevents use-before-available bugs | M |
| 2 | **LOADING/FAILED States for PluginFiber** — expand state machine from 4 to 6 states with async init support | Enables graceful async plugin startup, failure recovery without full dispose, better observability | S |
| 3 | **Scope-Bound Event Subscriptions** — `event_bus.on(event, handler, scope=fiber.scope)` auto-removes handlers on fiber dispose | Closes the largest "registration leak" gap; makes all event effects truly reversible | S |
| 4 | **Waterfall Event Dispatch** — add middleware-pattern dispatch to EventBus for tool execution pipeline interception | Enables pluggable pre/post tool execution hooks (approval, audit, transform), replaces hardcoded gates | L |
| 5 | **Provider/Consumer Ordering Protocol** — provider unload waits for consumer teardown before revoking services | Prevents "dependency disappears during cleanup" race; directly implements Cordis's UNLOADING + relied guard | M |

---

## 2. Paper Core Concepts (Relevant to Plugin Systems)

### 2.1 Revertible Effects

Every modification to shared state is modeled as `e : Γ → Γ × (Γ → Γ)` — a function that returns both the new state and an inverse. Inverses compose in reverse order (twisted composition). A runtime accumulator tracks all inverses; applying the accumulator restores the original state. The paper proves this LIFO recovery is exact when inverses are correct.

**Engineering takeaway**: Effect registration and cleanup must be colocated. The inverse is produced at the point of effect, not in a separate `deactivate` hook.

### 2.2 Reactive Coeffects

A component declares a dependency specification `d ⊆ K` (set of required services). The runtime evaluates `σ ⊧ d` after every context mutation. Changes are classified:
- **Activating**: deps were unsatisfied, now satisfied → start the component
- **Deactivating**: deps were satisfied, now unsatisfied → stop the component  
- **Neutral**: no change to satisfaction status

**Engineering takeaway**: Plugins should never manually check "is my dependency available." The runtime drives lifecycle transitions based on declared specs.

### 2.3 Fiber Lifecycle (Extended)

```
INACTIVE ─[deps satisfied]─→ RELOADING ─[effect iterator complete]─→ ACTIVE
   ↑                                                                      │
   │                                                    [deps changed or retired]
   │                                                                      ↓
   └────────[consumers all inactive]─── UNLOADING ←──────────────────────┘
                                            │
                                    [error during load]
                                            ↓
                                    INACTIVE(error)
```

Key features absent in LeapFlow:
1. **RELOADING** — async initialization with step-wise rollback on failure
2. **FAILED** capture — fiber retains error, can retry on dep change
3. **Withdrawal protocol** — provider enters UNLOADING but waits for all consumers to reach INACTIVE before executing its own inverse

### 2.4 Independence Condition

Cross-component unload safety requires "independence": two effects are independent when their forwards and inverses commute. For non-commutative resources (ordered middleware, priority handlers), explicit dependency edges or single-owner accumulation is required.

### 2.5 Unified Context & Observational Equivalence

The paper unifies effect tracking and dependency resolution into one recursive context type. Recovery need not be byte-identical — only observationally equivalent (interface-level behavior unchanged). This relaxation makes the model practical for real systems where heap layout, IDs, and handles change.

### 2.6 Confluence

Under assumptions of pairwise independence, acyclic dependency precedence, and bounded effect iterators: the final quiescent state is independent of load/unload scheduling order. This is the strongest guarantee — it means dynamic composition converges to the same result regardless of when plugins are loaded.

---

## 3. Detailed Dimension Analysis

### 3.1 Lifecycle Model Depth

**Cordis Model**: 6 states (PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED, plus FAILED branch). Loading supports async iteration with per-step inverse. Failure captures error and transitions to INACTIVE(error). A fiber can retry when dependencies change.

**LeapFlow Current** (`src/leapflow/domain/plugin_fiber.py`):
```python
_VALID_TRANSITIONS: dict[FiberState, set[FiberState]] = {
    FiberState.PENDING: {FiberState.ACTIVE},      # No LOADING intermediate
    FiberState.ACTIVE: {FiberState.UNLOADING},
    FiberState.UNLOADING: {FiberState.DISPOSED},
    FiberState.DISPOSED: set(),
}
```

Four states. PENDING→ACTIVE is instantaneous (no async init). No FAILED state — a plugin that fails during activation simply propagates the exception upward; there is no retry path.

**Gap Analysis**:
| Missing Phase | Impact on LeapFlow |
|--------------|-------------------|
| LOADING (async init) | Plugins with network setup (gateway adapters, signal sources) jump to ACTIVE before truly ready. Consumers may access uninitialized state. |
| FAILED (retryable) | A transient import failure (missing optional dep) permanently blocks a plugin until explicit human `plugin_enable`. No auto-retry on dependency availability. |
| Iterative effect with per-step rollback | LeapFlow's `reload()` is all-or-nothing — if re-import succeeds but `bind_runtime` fails, partial state may leak. |

**Value Assessment**: HIGH for LOADING/FAILED. These directly improve reliability of gateway adapters, marketplace plugins with network deps, and signal sources.

**Recommended Action**: Expand `FiberState` to 6 states. Add `async activate()` that transitions through LOADING, supports multi-step init, and captures failures into a FAILED state with retry trigger.

---

### 3.2 Scope Hierarchy & Composability

**Cordis Model**: Context tree with parent-child relationships. Services resolve through ancestor chain (nearest-first). Child scopes inherit parent services unless overridden. `ctx.isolate(name)` creates independent service bindings within a child scope.

**LeapFlow Current** (`src/leapflow/plugins/scoped_registry.py`):
```python
class ScopedToolRegistry:
    def __init__(self, registry: Any) -> None:
        self._registry = registry          # ONE underlying registry
        self._fibers: dict[str, PluginFiber] = {}  # FLAT dict, no hierarchy
```

Completely flat. All plugins share one registry, one DI namespace, one scope level. The only "hierarchy" is EffectScope parent-child, but it is used structurally (for cascading dispose), not for service resolution.

**Gap Analysis**:

What a hierarchical scope would buy LeapFlow:
1. **Workspace-scoped plugins**: Currently plugins are process-global (shared across all TUI sessions). A workspace scope could provide workspace-specific tool sets without cross-contamination.
2. **Session-scoped ephemeral plugins**: Turn-scoped "one-shot" tools (generated by the agent for a specific task) could live in a session-child scope and auto-dispose at session end.
3. **Service override per session**: A session could override the LLM provider (e.g., cheaper model for bulk tasks) without affecting other sessions.

**However**: LeapFlow's architecture deliberately pushes isolation to the session level via per-turn handler snapshots (`dict(registry.tool_handlers)` copied at turn start). This provides adequate runtime isolation for the most critical use case (in-flight safety). Full hierarchical scopes would add significant complexity.

**Value Assessment**: MEDIUM. The per-turn snapshot mechanism already provides the most important isolation property. Hierarchical scopes would primarily benefit workspace-scoped plugins — a real but less urgent need.

**Recommended Action**: Phase 1: Add an optional `scope_level` metadata to PluginFiber (`process` | `workspace` | `session`) for future routing. Phase 2: Only if multi-workspace plugin isolation becomes a concrete user demand, implement a two-level scope (process-global + workspace fork).

---

### 3.3 Service Injection & Dependency Resolution

**Cordis Model**: `ctx[key]` resolves through Proxy to nearest ancestor provider. Resolution is lazy, type-safe (via TypeScript module augmentation), and lifecycle-coupled (accessing a service from an inactive fiber throws INACTIVE_ACCESS).

**LeapFlow Current** (`src/leapflow/plugins/registry.py`):
```python
def bind_runtime(self, **deps: Any) -> None:
    for plugin in self._plugins.values():
        relevant = {k: v for k, v in deps.items() if k in plugin.dependencies}
        if relevant:
            plugin.bind_runtime(**relevant)
    self._last_bound_deps.update(deps)
```

Flat, eager push. All deps are `Any`. No lifecycle coupling — a plugin receives deps regardless of its fiber state. No validation that deps are actually available when the plugin tries to use them.

**Gap Analysis**:
| Cordis Capability | LeapFlow Equivalent | Gap |
|-------------------|--------------------|----|
| Lazy resolution (`ctx[key]`) | Eager push (`bind_runtime`) | No lazy fallback; dep must exist at bind time |
| Lifecycle-coupled access | No coupling | Plugin can access deps after its fiber is DISPOSED (stale reference) |
| Nearest-ancestor resolution | Global namespace | No override at different scope levels |
| Auto-reload on dep change | No mechanism | If a dep changes (e.g., LLM provider swapped), plugins are not notified |

**Real scenarios where this matters**:
- A gateway adapter depends on `gateway_server`. If the gateway restarts, the adapter holds a stale reference.
- `self_management` plugin depends on `llm_provider`. If the user changes model mid-session, the plugin is never notified.
- A plugin declares `memory_manager` dependency but is activated before memory is initialized. It receives `None` and must handle gracefully (current pattern: null checks throughout handler code).

**Value Assessment**: HIGH for dependency-availability gating (prevents null-dep errors). MEDIUM for auto-reload on dep change (real but infrequent scenario). LOW for hierarchical resolution (flat model is adequate for current use cases).

**Recommended Action**: 
1. Add satisfaction check: fiber stays PENDING until all declared deps are bound non-None.
2. Add dep-change notification: when `bind_runtime()` updates a key, notify fibers that declared it — trigger refresh.
3. Do NOT implement proxy-based lazy resolution — un-Pythonic and adds overhead.

---

### 3.4 Event Propagation Model (Waterfall)

**Cordis Model**: 5 dispatch modes (emit, parallel, serial, bail, waterfall). Waterfall is the middleware pattern — each handler receives a `next()` callback and can wrap/intercept/short-circuit. Tool execution flows through `tools/pre-execute → tools/execute → tools/post-execute → tools/result` waterfall events.

**LeapFlow Current**: `EventBus` with flat pub/sub only. Tool execution is direct dispatch:
```
Engine → ApprovalGate check (hardcoded) → handler(args) → result
```

No middleware pipeline. No way for plugins to intercept other tools.

**Gap Analysis**:
| Capability | Cordis | LeapFlow |
|-----------|--------|---------|
| Pre-execution interception | `tools/pre-execute` waterfall → allow/deny/ask | ApprovalGate (hardcoded, non-pluggable) |
| Around-dispatch (timeout/retry as middleware) | `tools/execute` waterfall | `asyncio.wait_for` (hardcoded in engine) |
| Post-execution transform | `tools/post-execute` waterfall | None |
| Result observation | `tools/result` emit | `TurnUsageTracker.record_tool_call()` (hardcoded) |
| Scope-filtered dispatch | Events filtered by context | All subscribers see all events |

**What waterfall would enable for LeapFlow**:
- ApprovalGate becomes a registered waterfall handler (not hardcoded) — other plugins can add approval logic.
- Audit/logging plugins can observe tool calls without engine modification.
- Transform plugins can modify tool results (e.g., redact sensitive output).
- Rate-limiting can be a middleware rather than a per-plugin concern.

**Value Assessment**: HIGH. This is the single most impactful extensibility enhancement. It transforms LeapFlow from "closed execution pipeline with plugin tools" to "extensible execution pipeline WITH plugin middleware."

**Recommended Action**: Implement waterfall dispatch as a new EventBus method. Refactor tool execution to flow through `tool.pre_execute → tool.execute → tool.post_execute` waterfall. ApprovalGate registers as a `pre_execute` handler.

---

### 3.5 Effectful Registration ("Registrations Are Effects")

**Cordis Model**: `ctx.effect(callback)` — every registration (service, event handler, child context) returns a disposer. The disposer is tracked in the fiber's accumulator. On fiber dispose, all accumulated disposers run in reverse order.

**LeapFlow Current** (`src/leapflow/domain/effect_scope.py`):
```python
def effect(self, cleanup: Callable[[], None]) -> None:
    """Register a cleanup callback. Raises if scope is not active."""
    self._effects.append(cleanup)
```

LeapFlow's EffectScope implements this principle. Registration + cleanup are colocated:
```python
# From scoped_registry.py
self._registry.register(plugin)
fiber.scope.effect(_cleanup)  # cleanup registered at point of registration
```

**Where LeapFlow already embodies this**: Tool registration, plugin registration, fiber lifecycle. The `ScopedToolRegistry.scoped_register()` is a textbook "registration as effect" implementation.

**Where gaps exist**:
1. **EventBus subscriptions** — NOT scope-bound. If a plugin subscribes to an event during activation, that subscription is never automatically cleaned up on fiber dispose. The plugin must manually track and unsubscribe.
2. **ActiveSignalSource lifecycle** — NOT fiber-managed (explicitly noted in source: "Future extension: this manager can be integrated with EffectScope"). Signal sources are managed by `ActiveSourceManager.dispose()`, disconnected from the plugin fiber system.
3. **Cross-cutting runtime gates** — The registry holds references to gates (`_file_read_gate`, `_desktop_gate`) that are set imperatively and never "unset" if their owner is disposed.
4. **Late-bound tools** — `register_late_tool()` adds tools to the registry but the cleanup is fiber-bound only if called through `scoped_register_late_tool()`. Direct calls leave orphaned tools.

**Value Assessment**: HIGH for EventBus scope-binding (closes the most common leak). MEDIUM for signal source integration (noted but not urgent). LOW for gate references (gates outlive individual plugins by design).

**Recommended Action**: 
1. Add `scope` parameter to EventBus subscription that auto-registers cleanup on the given EffectScope.
2. Integrate ActiveSourceManager with fiber lifecycle (as the source code already recommends).

---

### 3.6 Hot-Reload Formal Semantics

**Cordis Formal Guarantee**: Dispose + re-compose preserves system invariants:
- No dangling references (all consumers of old provider are notified and re-resolved)
- No orphaned effects (accumulator fully runs before new fiber starts)
- Provider waits for consumers to teardown before revoking service
- New fiber's committed view resolves against current context (not stale)

**LeapFlow Current** (`src/leapflow/plugins/scoped_registry.py` `reload()`):
```python
# 1. Dispose old fiber (EffectScope cleanup runs unregister)
old_fiber.begin_unload()
old_fiber.dispose()

# Belt-and-suspenders defensive cleanup
self._unregister_tools(plugin_id, old_tool_names)

# 2. Re-import the plugin module
fresh_plugin = self._load_fresh_plugin(plugin_id, module_path)

# 3. Create new fiber and register
new_fiber = self.create_fiber(plugin_id)
self.scoped_register(fresh_plugin, new_fiber)
new_fiber.activate()

# 4. Publish tools + bump version
self._registry.publish_plugin_tools(fresh_plugin)

# 5. Re-inject deps
if self._registry.last_bound_deps:
    self._registry.bind_runtime(**self._registry.last_bound_deps)
```

**Invariants that LeapFlow's reload can violate**:

1. **In-flight request safety**: SAFE — per-turn snapshot ensures old handlers stay valid for in-flight turns. New turns get new handlers. This is well-designed.

2. **Cached references**: PARTIALLY SAFE — engine caches are invalidated by version bump. However, any module that imported a handler function directly (bypassing the registry) holds a stale reference. The architecture discourages this but doesn't prevent it.

3. **Cross-plugin dependencies**: UNSAFE — if Plugin B depends on Plugin A's service, and Plugin A is reloaded, Plugin B is never notified. It continues using stale references to A's old state. The `bind_runtime(**last_bound_deps)` re-injects into the NEW plugin A but does not re-inject into B with the new A's outputs.

4. **Provider-consumer ordering**: UNSAFE — LeapFlow has no "wait for consumers" protocol. If Plugin A provides a service consumed by Plugin B, reloading A immediately removes the service; B's in-flight cleanup (if it had any) would access a missing service.

5. **Partial failure rollback**: PARTIALLY SAFE — if `_load_fresh_plugin` fails, old tools are already removed (defensive unregister ran). The plugin is left in a "neither old nor new" state. The "belt-and-suspenders" defensive cleanup is an acknowledgment that the atomic guarantee is weak.

**Value Assessment**: HIGH for cross-plugin dependency notification on reload. MEDIUM for provider-consumer ordering. LOW for partial failure (existing belt-and-suspenders is adequate).

**Recommended Action**:
1. On reload of plugin A, identify all plugins that declared A's outputs as dependencies → trigger their refresh.
2. Implement provider withdrawal: old fiber enters UNLOADING → notifies consumers → waits for consumer teardown → then runs own effects.

---

### 3.7 Spatiotemporal Composability in Multi-Instance Scenarios

**Cordis Model**: Designed for multiple independent composition trees coexisting. Each tree has its own root context, own service namespace, own lifecycle independence.

**LeapFlow Current** (from lifecycle doc §4.9):
> Plugins are **process-global** on the daemon. The `ToolPluginRegistry` is a module-level singleton; `ScopedToolRegistry` wraps it. All TUI sessions connected to the same daemon share one plugin set.

**Analysis**: LeapFlow's multi-TUI model has these properties:
- Plugin registration/unregistration affects ALL sessions
- Per-turn handler snapshots provide isolation for in-flight turns
- Trust accrual is global (all sessions contribute)
- No workspace-scoped plugin set exists

**Would Cordis-style scope forks help?**

Yes, for specific scenarios:
1. **Workspace-specific tools**: A development workspace might need `git_tools` while a writing workspace needs `grammar_tools`. Currently both get all tools.
2. **Session-scoped generated plugins**: An agent generates a one-shot tool for a specific task. Today it's installed globally and must be explicitly removed.
3. **Multi-tenant isolation**: If LeapFlow supports multiple users, each needs independent plugin state.

**But**: The current per-turn snapshot mechanism already provides the most critical isolation property (in-flight safety). Full scope forks would require significant refactoring of the singleton registry.

**Value Assessment**: MEDIUM. Real value for workspace-scoped tools; current design is adequate for MVP multi-session.

**Recommended Action**: Add `scope_level` metadata to fibers. When `scope_level="workspace"`, the fiber is only included in handler snapshots for turns from that workspace. Implementation: a lightweight filter in `_unified_tool_handlers()` keyed by workspace ID.

---

### 3.8 Security Boundaries & Plugin Isolation

**Cordis Model**: Scope isolation provides capability-based security. A child context can only access services explicitly provided by ancestors. `ctx.intercept(key, config)` attaches metadata (permissions, quotas) to service access. The proxy enforces: undeclared access → UNDECLARED_ACCESS error; inactive access → INACTIVE_ACCESS error.

**LeapFlow Current**: Multi-layered but different approach:
- `SandboxHost` (subprocess JSON-RPC) for untrusted plugin execution isolation
- `ApprovalGate` for mutation approval
- Progressive Trust (DRAFT→PRODUCTION) for privilege graduation
- Risk classification (`security/risk.py`) gates mutation actions

**Comparison**:
| Security Property | Cordis Approach | LeapFlow Approach |
|-------------------|----------------|-------------------|
| Code isolation | In-process (same Node.js context) | Subprocess (stronger) |
| Capability restriction | Scope-based service visibility | Approval gates + trust levels |
| Undeclared access prevention | Proxy throws UNDECLARED_ACCESS | Runtime `Any` — no enforcement |
| Resource quotas | Interceptor metadata on service access | Not implemented (noted in gaps) |
| Permission gradation | Binary (can access or cannot) | 4-level trust + per-action risk |

**Key insight**: LeapFlow's isolation model is **stronger for untrusted code** (subprocess beats in-process) but **weaker for capability restriction** (no enforcement that a plugin only accesses what it declared).

**Does Cordis suggest lighter-weight isolation?**

For PRODUCTION-trust plugins (already trusted, running in-process): Yes. Scope-based capability attenuation could restrict a trusted plugin's access to only its declared dependencies, without subprocess overhead. Currently, once a plugin is in-process, it can `import leapflow.plugins; get_registry()` and access anything.

**Value Assessment**: LOW-MEDIUM. The current model works. Scope-based capability attenuation would be defense-in-depth for in-process plugins but isn't a pressing need given Progressive Trust gates installation.

**Recommended Action**: For in-process plugins, enforce that `bind_runtime()` only delivers declared dependencies (already implemented). Longer term, consider making the registry accessor require a capability token rather than being freely importable.

---

### 3.9 Standards & Policy Design Insights

The paper establishes several design rules that make the system work. Assessment against LeapFlow:

| Cordis Design Rule | LeapFlow Status | Gap |
|-------------------|-----------------|-----|
| **"Effects must be reversible"** (every registration returns an inverse) | ✅ Enforced for tool registration via EffectScope. ❌ NOT enforced for EventBus subscriptions, runtime gate assignments, or sys.modules entries. | MEDIUM gap |
| **"Services are resolved nearest-first"** (hierarchical scope chain) | ❌ No hierarchy. Flat global namespace. | N/A — different design choice |
| **"Dispose propagates depth-first"** (children before parent) | ✅ Enforced: `EffectScope.dispose()` disposes children in reverse order, then own effects. | No gap |
| **"Provider waits for consumers"** (UNLOADING + relied guard) | ❌ No protocol. Provider disposes immediately; consumers may access stale state during their own teardown. | HIGH gap |
| **"Committed view tracks resolution source"** (deps point to specific provider fibers) | ❌ LeapFlow deps are `Any` values, not typed references to provider fibers. No detection of "same dep, different provider." | MEDIUM gap |
| **"Failure is local"** (failed fiber does not crash siblings) | ✅ Enforced: EffectScope wraps each cleanup in try/except. PluginFiber failure doesn't propagate. | No gap |
| **"Configuration reconciliation drives loading"** (declarative target state) | ❌ Loading is imperative (`register()`, `activate()`). No declarative "desired plugin set." | MEDIUM gap |
| **"Effect and inverse are colocated"** (produced at same point) | ✅ Mostly enforced: `scope.effect(cleanup)` at registration site. | Minimal gap |
| **"Dispose is idempotent"** (safe to call multiple times) | ✅ Enforced: `EffectScope.dispose()` checks state before running. | No gap |
| **"Async effects have inertia"** (once launched, must complete before unload) | ❌ No concept of in-flight effects. `dispose()` is synchronous and immediate. An async operation started by a plugin may outlive its fiber. | HIGH gap |

**Most critical missing policies**:
1. Provider-consumer ordering (HIGH)
2. Async effect inertia / graceful drain on unload (HIGH)
3. Scope-bound event subscriptions (MEDIUM)

---

### 3.10 Practical Upgrade Opportunities

For each identified gap, practical assessment:

| Opportunity | Value to LeapFlow | Complexity | Aligns with Philosophy? | Phase |
|------------|-------------------|------------|------------------------|-------|
| Dependency-driven fiber activation | HIGH — eliminates null-dep bugs, enables auto-start | M — need notification mechanism in bind_runtime | ✅ Signal-Driven, Progressive Trust | P1 |
| LOADING/FAILED fiber states | HIGH — async init, failure recovery | S — state machine expansion, existing tests cover transitions | ✅ Graceful Degradation | P1 |
| Scope-bound EventBus subscriptions | HIGH — closes largest effect-leak gap | S — add `scope` param to subscribe(), register cleanup | ✅ Effectful Registration | P1 |
| Waterfall event dispatch + tool pipeline | HIGH — extensibility breakthrough | L — new dispatch mode, refactor tool execution | ✅ Protocol over ABC, Config-Driven | P2 |
| Provider-consumer ordering on unload | MEDIUM — prevents stale-dep-during-teardown race | M — track provider→consumer edges, add wait | ✅ Graceful Degradation | P2 |
| Async effect support in EffectScope | MEDIUM — proper drain for network-bound cleanup | S — `async dispose()`, async cleanup list | ✅ Industrial Robustness | P1 |
| Declarative plugin config (`plugins.yaml`) | MEDIUM — configuration reconciliation | M — new config layer, loader integration | ✅ Config-Driven Behavior | P2 |
| Workspace-scoped plugin filtering | MEDIUM — multi-workspace isolation | M — scope metadata + handler filter | ✅ Multi-instance support | P3 |
| `provides` declaration on ToolPlugin | MEDIUM — forward-looking service graph | S — add optional Protocol property | ✅ Protocol over ABC | P1 |
| Per-plugin config schema validation | LOW-MEDIUM — prevents misconfiguration | M — JSON Schema or Pydantic validation layer | ✅ Config-Driven | P3 |

---

## 4. Upgrade Priority Matrix

| # | Opportunity | Value | Effort | Philosophy Aligned? | Recommended Phase |
|---|------------|:-----:|:------:|:-------------------:|:-----------------:|
| 1 | Dependency-driven fiber activation | H | M | ✅ | P1 |
| 2 | LOADING/FAILED fiber states | H | S | ✅ | P1 |
| 3 | Scope-bound EventBus subscriptions | H | S | ✅ | P1 |
| 4 | Async cleanup support in EffectScope | M | S | ✅ | P1 |
| 5 | `provides` declaration on ToolPlugin Protocol | M | S | ✅ | P1 |
| 6 | Waterfall dispatch + tool execution pipeline | H | L | ✅ | P2 |
| 7 | Provider-consumer ordering protocol | M | M | ✅ | P2 |
| 8 | Declarative plugin composition config | M | M | ✅ | P2 |
| 9 | Cross-plugin dep-change notification on reload | M | M | ✅ | P2 |
| 10 | Workspace-scoped plugin filtering | M | M | ✅ | P3 |
| 11 | Per-plugin config schema validation | L-M | M | ✅ | P3 |
| 12 | Committed view (track which fiber provides each dep) | L-M | M | ✅ | P3 |
| 13 | Full hierarchical scope tree | L | XL | ⚠️ Occam's Razor tension | SKIP (see §6) |
| 14 | Proxy-based lazy DI resolution | L | L | ❌ Un-Pythonic | SKIP |
| 15 | Isolation scopes (`ctx.isolate`) | L | L | ⚠️ Over-engineering | SKIP (Phase 4?) |

---

## 5. Concrete Design Sketches

### 5.1 Dependency-Driven Fiber Activation

**Problem**: Fibers currently activate unconditionally. A plugin with `dependencies = ["memory_manager", "llm_provider"]` is activated even if those deps are None.

**Design**:

```python
# Enhanced PluginFiber states
class FiberState(enum.Enum):
    PENDING = "pending"       # Created, deps not yet satisfied
    LOADING = "loading"       # Deps satisfied, async init in progress
    ACTIVE = "active"         # Fully operational
    UNLOADING = "unloading"   # Teardown in progress
    FAILED = "failed"         # Init failed; retryable on dep change
    DISPOSED = "disposed"     # Terminal

# New transitions
_VALID_TRANSITIONS = {
    FiberState.PENDING: {FiberState.LOADING, FiberState.DISPOSED},
    FiberState.LOADING: {FiberState.ACTIVE, FiberState.FAILED, FiberState.UNLOADING},
    FiberState.ACTIVE: {FiberState.UNLOADING},
    FiberState.UNLOADING: {FiberState.PENDING, FiberState.DISPOSED},
    FiberState.FAILED: {FiberState.LOADING, FiberState.DISPOSED},
    FiberState.DISPOSED: set(),
}
```

**Activation flow in ScopedToolRegistry**:

```python
def _check_satisfaction(self, fiber: PluginFiber) -> bool:
    """Check if all declared deps are bound non-None."""
    plugin = self._registry.get_plugin(fiber.plugin_id)
    if plugin is None:
        return False
    bound = self._registry.last_bound_deps
    return all(
        k in bound and bound[k] is not None
        for k in plugin.dependencies
        if not k.startswith("optional_")  # Convention: optional_ prefix
    )

def _on_deps_changed(self, changed_keys: set[str]) -> None:
    """Called after bind_runtime updates deps. Checks all PENDING/FAILED fibers."""
    for plugin_id, fiber in self._fibers.items():
        if fiber.state in (FiberState.PENDING, FiberState.FAILED):
            if self._check_satisfaction(fiber):
                self._activate_fiber(fiber)
        elif fiber.state == FiberState.ACTIVE:
            if not self._check_satisfaction(fiber):
                fiber.begin_unload()  # Dep disappeared → deactivate
```

**Integration point**: `ToolPluginRegistry.bind_runtime()` emits a notification after updating deps, triggering `_on_deps_changed()` in the scoped registry.

**Backward compatibility**: Plugins with empty `dependencies` lists auto-satisfy immediately (PENDING → LOADING → ACTIVE in one cycle). Existing behavior preserved.

---

### 5.2 Scope-Bound Event Subscriptions

**Problem**: EventBus subscriptions are not tracked as effects. A plugin subscribing during activation leaks the handler after fiber dispose.

**Design**:

```python
# In EventBus (or a wrapper):
def subscribe(
    self, 
    event_type: str, 
    handler: Callable, 
    *, 
    scope: Optional[EffectScope] = None,
    priority: int = 0,
) -> Callable[[], None]:
    """Subscribe to an event. Returns unsubscribe callable.
    
    If scope is provided, the subscription is automatically cleaned up
    when the scope is disposed (effectful registration pattern).
    """
    # ... register handler ...
    
    def _unsubscribe():
        self._handlers[event_type].discard((priority, handler))
    
    if scope is not None:
        scope.effect(_unsubscribe)
    
    return _unsubscribe
```

**Usage in a plugin**:
```python
# During plugin activation:
def apply(self, fiber: PluginFiber, event_bus: EventBus):
    # Subscription is automatically cleaned up when fiber disposes
    event_bus.subscribe("tool.pre_execute", self._on_pre_execute, scope=fiber.scope)
```

**Impact**: Every event subscription becomes a tracked, reversible effect. No manual cleanup needed. Fiber dispose automatically unsubscribes all handlers registered with that fiber's scope.

---

### 5.3 Waterfall Event Dispatch for Tool Execution

**Problem**: Tool execution is a closed pipeline. No plugin can intercept, transform, or gate another plugin's tools.

**Design**:

```python
class WaterfallPipeline:
    """Middleware-pattern event dispatch. Handlers compose around next()."""
    
    def __init__(self) -> None:
        self._handlers: list[tuple[int, Callable]] = []  # (priority, handler)
    
    def use(self, handler: Callable, *, priority: int = 0, scope: Optional[EffectScope] = None):
        """Register a middleware. Auto-removed when scope disposes."""
        entry = (priority, handler)
        self._handlers.append(entry)
        self._handlers.sort(key=lambda x: x[0])
        
        if scope is not None:
            scope.effect(lambda: self._handlers.remove(entry) if entry in self._handlers else None)
    
    async def execute(self, context: dict, final: Callable) -> Any:
        """Run the pipeline. Each handler calls next() to proceed."""
        handlers = [h for _, h in self._handlers]
        
        async def _chain(idx: int, ctx: dict) -> Any:
            if idx >= len(handlers):
                return await final(ctx)
            return await handlers[idx](ctx, lambda c=ctx: _chain(idx + 1, c))
        
        return await _chain(0, context)
```

**Tool execution refactored**:
```python
# In engine, replace direct handler(args) with:
pre_execute_pipeline = WaterfallPipeline()   # approval, rate-limit, audit
execute_pipeline = WaterfallPipeline()        # timeout, retry, dispatch
post_execute_pipeline = WaterfallPipeline()   # transform, redact, observe

# ApprovalGate registers as middleware:
pre_execute_pipeline.use(approval_gate.check, priority=0, scope=system_scope)

# Audit plugin registers as observer:
post_execute_pipeline.use(audit_plugin.observe, priority=100, scope=audit_fiber.scope)
```

**Key properties**:
- ApprovalGate becomes a registered handler, not hardcoded engine logic
- Plugins can add pre/post execution middleware
- Middleware registration is scope-bound → auto-cleanup on fiber dispose
- Priority ordering provides deterministic execution sequence
- `final` callback is the actual tool handler dispatch

---

### 5.4 Provider-Consumer Ordering Protocol

**Problem**: When Plugin A (provider) is reloaded, Plugin B (consumer of A's service) may access stale state during its own teardown.

**Design**:

```python
@dataclass
class FiberDependencyGraph:
    """Tracks which fibers provide services consumed by other fibers."""
    
    # provider_fiber_id → set of consumer_fiber_ids
    _providers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    # consumer_fiber_id → set of provider_fiber_ids
    _consumers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    
    def record_binding(self, provider_id: str, consumer_id: str) -> None:
        self._providers[provider_id].add(consumer_id)
        self._consumers[consumer_id].add(provider_id)
    
    def get_consumers(self, provider_id: str) -> set[str]:
        return self._providers.get(provider_id, set())
```

**Withdrawal protocol in ScopedToolRegistry**:
```python
async def dispose_with_ordering(self, plugin_id: str) -> None:
    """Dispose a provider fiber, respecting consumer ordering.
    
    1. Mark provider as UNLOADING (stops providing new bindings)
    2. Notify all consumers → they begin their own unload
    3. Wait for all consumers to reach PENDING/DISPOSED
    4. Execute provider's own EffectScope disposal
    """
    fiber = self._fibers[plugin_id]
    fiber.begin_unload()
    
    # Find consumers that depend on this provider's outputs
    consumers = self._dep_graph.get_consumers(plugin_id)
    
    # Trigger consumer deactivation
    for consumer_id in consumers:
        consumer_fiber = self._fibers.get(consumer_id)
        if consumer_fiber and consumer_fiber.state == FiberState.ACTIVE:
            consumer_fiber.begin_unload()
            consumer_fiber.dispose()
    
    # Now safe to dispose provider
    fiber.dispose()
```

**When this activates**: Only during explicit `reload()` or `dispose_plugin()` when the plugin has registered `provides`. For plugins without `provides`, current immediate dispose is unchanged.

---

### 5.5 Async EffectScope Cleanup

**Problem**: `EffectScope.dispose()` is synchronous. Plugins with async resources (network connections, signal sources, background tasks) cannot properly drain during dispose.

**Design**:

```python
class EffectScope:
    def __init__(self, name: str, *, parent: Optional["EffectScope"] = None) -> None:
        # ... existing init ...
        self._async_effects: list[Callable[[], Awaitable[None]]] = []
    
    def effect(self, cleanup: Callable[[], None]) -> None:
        """Register a sync cleanup callback."""
        # ... existing implementation ...
    
    def async_effect(self, cleanup: Callable[[], Awaitable[None]]) -> None:
        """Register an async cleanup callback (for network/IO teardown)."""
        if self.state != ScopeState.ACTIVE:
            raise RuntimeError(...)
        self._async_effects.append(cleanup)
    
    async def async_dispose(self) -> None:
        """Async dispose: runs async effects first, then sync effects (both LIFO).
        
        Falls back to sync dispose() if called in non-async context.
        """
        if self.state == ScopeState.DISPOSED:
            return
        self.state = ScopeState.DISPOSING
        
        # Children first (async)
        for child in reversed(self._children):
            await child.async_dispose()
        
        # Async effects in reverse order
        for cleanup in reversed(self._async_effects):
            try:
                await asyncio.wait_for(cleanup(), timeout=5.0)
            except Exception as exc:
                logger.warning("Async effect cleanup failed in scope '%s': %s", self.name, exc)
        
        # Sync effects in reverse order (existing logic)
        for cleanup in reversed(self._effects):
            try:
                cleanup()
            except Exception as exc:
                logger.warning("Effect cleanup failed in scope '%s': %s", self.name, exc)
        
        self._effects.clear()
        self._async_effects.clear()
        self._children.clear()
        self.state = ScopeState.DISPOSED
```

**Key constraint**: Timeout on each async cleanup (5s default, configurable). Aligns with Cordis's "inertia" concept — effects that have been launched must complete, but within a bounded time.

---

## 6. What to SKIP (And Why)

### 6.1 Full Hierarchical Scope Tree — SKIP

**Cordis feature**: Recursive context tree where each node inherits parent services, can override, and manages its own children.

**Why skip**: LeapFlow's flat model with per-turn snapshots already provides the critical isolation property (in-flight safety). A full scope tree would require rewriting the singleton registry pattern, introducing complex service resolution logic, and adding performance overhead for every dependency access. The benefit (workspace-scoped plugins) can be achieved more simply with scope metadata + filter (§5 sketch).

**Occam's Razor**: The simplest correct solution is a `scope_level` tag on fibers + a filter in the handler snapshot builder, not a recursive tree with resolution chain.

### 6.2 Proxy-Based Lazy DI Resolution — SKIP

**Cordis feature**: ES Proxy on `ctx` that lazily resolves services through scope chain on property access.

**Why skip**: Python has no performant equivalent to ES Proxy for attribute interception. `__getattr__` overrides are possible but add overhead to every attribute access, break IDE tooling, and are un-Pythonic. LeapFlow's explicit `bind_runtime(**deps)` is idiomatic Python, readable, and debuggable.

**LLM-Native Design**: An LLM reasoning about code benefits from explicit dependency injection (visible in function signatures) over hidden resolution (invisible magic).

### 6.3 Service Isolation (`ctx.isolate`) — SKIP (for now)

**Cordis feature**: Create a child scope where a specific service resolves independently from the parent.

**Why skip**: The primary use case (multi-agent with different LLM providers) is not yet a production LeapFlow scenario. When it becomes one, per-session overrides through the existing config system (`leap config --session`) are a simpler mechanism. Full isolation scopes add architectural complexity without current demand.

**Revisit when**: Multiple concurrent agents with different tool sets become a supported scenario.

### 6.4 Confluence Guarantee (Load-Order Independence) — SKIP formal proof

**Cordis feature**: Formal proof that final state is independent of plugin load order, under assumptions.

**Why skip enforcement**: LeapFlow's plugins are discovery-order-dependent by design (built-ins have fixed order; third-party load in filesystem order). The assumptions required for confluence (pairwise independence, total provision) are hard to enforce in practice — Python's ambient authority and shared mutable state make true independence impractical to guarantee.

**Practical mitigation**: The per-turn snapshot mechanism makes load order irrelevant for tool dispatch. Only registration-time conflicts (duplicate IDs) are order-sensitive, and those are caught immediately.

### 6.5 Configuration Reconciliation (Declarative Desired State) — DEFER to P3

**Cordis feature**: `cordis.yml` declares the desired plugin set; the loader reconciles current state to desired state.

**Why defer**: Valuable but lower priority than the runtime improvements. LeapFlow's current imperative registration (`discover_builtin()` + `register()`) works for the boot path. A `plugins.yaml` section in config would improve the install/enable/disable UX but doesn't unblock any critical use case.

### 6.6 Observational Equivalence Formalization — SKIP

**Cordis feature**: Formal definition of when two states are "equivalent enough" post-recovery.

**Why skip**: LeapFlow's dispose is pragmatic (run all cleanups; log failures). A formal equivalence relation adds theoretical elegance but no practical value — LeapFlow plugins don't need to prove they restored the exact state, they need to not leak handlers/subscriptions/resources.

---

## 7. Conclusion & Strategic Recommendation

### Strategic Assessment

The Cordis paper's greatest insight for LeapFlow is not the full formal apparatus (which targets a TypeScript in-process composition model), but three specific engineering principles:

1. **Reactive dependency lifecycle** — the runtime, not the plugin, decides when to activate/deactivate based on dependency satisfaction. This eliminates null-dep bugs and enables graceful degradation.

2. **Waterfall composition for execution pipelines** — transforming the tool execution path from a closed pipeline to an open middleware chain unlocks an entirely new class of plugins (guards, auditors, transformers, rate limiters) without engine modification.

3. **Provider-consumer ordering during teardown** — the insight that a provider must not revoke its service until consumers have completed their own teardown is a correctness property LeapFlow currently violates.

### Recommended Execution Path

**Phase 1 (2-3 weeks)**: Foundation improvements
- Expand PluginFiber to 6 states (LOADING, FAILED)
- Implement dependency-driven activation in ScopedToolRegistry
- Add scope-bound EventBus subscriptions
- Add async_effect support to EffectScope
- Add `provides: list[str]` to ToolPlugin Protocol (optional, backward-compatible)

**Phase 2 (3-4 weeks)**: Extensibility breakthrough
- Implement WaterfallPipeline for tool execution
- Refactor ApprovalGate from hardcoded check to registered middleware
- Add provider-consumer ordering protocol
- Add cross-plugin dep-change notification on reload
- Optional: declarative `plugins:` config section

**Phase 3 (2-3 weeks, when demanded)**: Isolation & configuration
- Workspace-scoped plugin filtering
- Per-plugin config schema validation
- Committed view (track provider identity per dep binding)

### Alignment with LeapFlow Design Philosophy

| Principle | How upgrades align |
|-----------|-------------------|
| Signal-Driven Intelligence | Dependency satisfaction IS a signal; reactive activation IS signal-driven lifecycle |
| Progressive Trust | Trust levels compose with fiber states — DRAFT plugins in LOADING state can timeout without harming the system |
| Occam's Razor | Each upgrade is the simplest mechanism that solves a real problem; full scope tree is explicitly rejected |
| LLM-Native Design | Waterfall pipelines are declarative composition; plugins declare intent, runtime composes behavior |
| Industrial Robustness | Async cleanup, provider ordering, FAILED state — all improve robustness under adverse conditions |

### Final Note

The Cordis paper provides a rigorous theoretical foundation for what LeapFlow has partially built through engineering pragmatism. The key upgrade is not "adopt Cordis" but "adopt Cordis's three missing principles" (reactive deps, waterfall composition, provider ordering) while preserving LeapFlow's strengths (Progressive Trust, subprocess isolation, per-turn snapshots, Occam's Razor simplicity). The result would be a plugin system that is formally more sound, practically more extensible, and architecturally ready for the self-evolving agent harness future the paper envisions.

---

## Appendix: Key File References

| File | Role in Analysis |
|------|-----------------|
| `src/leapflow/domain/effect_scope.py` | Current LIFO effect tracking implementation |
| `src/leapflow/domain/plugin_fiber.py` | Current 4-state lifecycle machine |
| `src/leapflow/plugins/scoped_registry.py` | Current reload, adopt, dispose mechanisms |
| `src/leapflow/plugins/registry.py` | Current flat DI via bind_runtime() |
| `src/leapflow/plugins/protocol.py` | ToolPlugin Protocol (upgrade target for `provides`) |
| `src/leapflow/perception/active_signal_source.py` | ActiveSourceManager — noted fiber integration gap |
| `src/leapflow/learning/compatibility/taxonomy.py` | Pluggability boundary taxonomy |
| `docs/plugins/plugin_lifecycle_management.md` | Current lifecycle strategy (baseline for upgrades) |
| `docs/plugins/third_party_plugin_development.md` | Current developer contract |
| `temp/deepseek_harness/cordis_spatiotemporal_composability_paper_report.md` | Paper analysis |
| `temp/deepseek_harness/deepseek_harness_compatibility_analysis.md` | DSH gap analysis |
