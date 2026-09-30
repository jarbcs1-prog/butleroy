# ARCHITECTURE

Four planes. One invariant. Read DECISIONS.md first — this document assumes it.

```
┌─ PLANE A · RESIDENT DAEMON ────────────────── always on · ~300 MB RAM · 0 VRAM ─┐
│  pystray tray · Ctrl+Alt+Space · wake word · Telegram long-poll                  │
│  named-pipe server · policy gate · VRAM arbiter · audit log · scheduler        │
│  Obsidian bridge · workspace-manifest resolver                                   │
└──────────────────────┬──────────────────────────────────────────────────────────┘
                       │ request (JSON)                    ▲ event / result
                       ▼                                    │
┌─ PLANE B · LOCAL BRAIN ────────────────── on demand · ≤7 GiB VRAM · killed idle ┐
│  Qwen3.5-9B + mmproj, child process, 90 s TTL                                    │
│  intent · brainstorm · decompose · teach · emit tool-call · act as LLMJudge     │
│  capabilities: knowledge · administration · delegate() · teach()                 │
│  NOT capabilities: direct code editing in a target project  (D16)                │
└──────────────────────┬──────────────────────────────────────────────────────────┘
                       │ assignTask / cancelTask / intervene
                       ▼
┌─ PLANE C · SUPERVISOR ──────────────────── Node/TS · vendored fork · no LLM ───┐
│  AgentRegistry · HeartbeatMonitor · LoopDetector(+LLMJudge) · Watchdog          │
│  CircuitBreaker · RecoveryEngine · AuditTrail(verifyIntegrity) · EventBus       │
│  intervention ladder: observe→warn→throttle→pause→restart→replace→escalate      │
│  transport: NAMED PIPES (express API removed — D12)                             │
└──────────────────────┬──────────────────────────────────────────────────────────┘
                       │ RuntimeSpec
                       ▼
┌─ PLANE D · WORKERS ───────────────── isolated · disposable · disposable model ──┐
│  opencode v2.0.19 subprocess  →  role definitions (templates, D21)              │
│  python-subprocess tool workers (faster-whisper · piper · openWakeWord)         │
│  may use cloud providers — this is the ONLY plane that may (D11)                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## The invariant

> The assistant must never take resources it needs from the user.

The daemon is always resident; only the model cycles. This is what makes "24/7" cost ~300 MB
instead of ~7 GB — and it is the difference between an assistant that is always on and an
assistant that permanently owns the machine.

---

## Why the planes are separate processes

The planner and the supervisor have **different clocks and different failure costs**:

- If Plane B crashes (OOM, model fault), you lose the conversation — but supervision of an
  in-flight six-agent build must continue uninterrupted.
- If Plane C restarts, you must not lose worker checkpoints or break the audit chain.
- Fusing them means the thing watching the workers dies every time the GPU faults.

Plane C also needs no GPU at all (D12: `LLMJudge` is optional and the supervision loop is
pure). It runs while you sleep. The brain does not.

---

## Transport

All inter-plane IPC is **Windows named pipes** via `multiprocessing.connection` — no pywin32
required (verified working). **No TCP ports anywhere in the system.**

| From → To | Pipe | Payload |
|---|---|---|
| A → B | `\\.\pipe\locala.brain.req` | request JSON, cancellable |
| B → A | `\\.\pipe\locala.brain.resp` | streaming deltas, tool-calls |
| A → C | `\\.\pipe\locala.sup.req` | assignTask, cancelTask, intervene, checkpoints |
| C → A | `\\.\pipe\locala.sup.evt` | heartbeat, loop, breaker, recovery, audit events |
| A → UI  | local UI thread / in-process | overlay render (never a socket) |

---

## The VRAM arbiter

D1 is a budget; the arbiter is the mechanism. **The arbiter is the single authority on GPU
memory. Models acquire leases; they do not request permissions.**

```
acquire(need_bytes, holder):
    free = nvidia_smi.free_vram()
    if free - need_bytes < RESERVE:          # RESERVE ≈ 1.5 GiB held back for the OS + user
        preempt_self()                        # kill our own lowest-priority model first
    if still insufficient:
        return DENIED                          # tell the user why; do not proceed
    lease = Lease(holder, need_bytes, ttl=90s)
```

Binding properties:
- **Preemption always kills the assistant's model, never the user's workload.** The assistant is
  the only tenant that can be evicted without asking its own permission.
- If you start a game or a CUDA job, the arbiter sees free VRAM drop and the assistant yields
  before it causes you a stutter.
- **Lease TTL expiry kills the process.** That is what actually returns VRAM on Windows (D3).
- Denials are surfaced to the user, not silently retried.

---

## Authority model

Deterministic code in the policy gate classifies every proposed tool call. The model proposes;
**the gate disposes.**

```
model emits tool_call
        │
        ▼
  policy gate ──► path allowlist / denylist     (D10, D21)
        │       ──► tier lookup (T0/T1/T2/T3)  (D13)
        │       ──► channel check              (D13 rule 2)
        │       ──► session check              (D13 rule 1)
        ▼
  authorize ──► execute ──► audit log (append-only, hash-chained)
```

- **Path allowlist** is empty by default. Every directory the assistant can touch must be granted
  via a **workspace manifest**. No manifest, no capability.
- **Deny-path is absolute**: `C:\Windows`, `C:\Program Files`, the assistant's own `policy/`
  directory, `*/.git/*` internals, and any path in a manifest marked `danger_level: critical`
  requires per-action confirmation regardless of tier.
- **Unattended cap**: no interactive session → only T0/T1 execute. T2/T3 queue.
- **Channel rule**: Telegram may request but never authorize T3.

### Workspace manifest

The only sanctioned way for the assistant to know a project exists. Explicit, user-approved,
human-readable:

```yaml
root: F:\some\project
language: typescript
stack: [node, pnpm, vitest]
test_command: pnpm test
danger_level: standard          # standard | sensitive | critical
allow_tiers: [T0, T1, T2]       # T3 still requires per-action confirmation
agent_definitions: ./agents/    # role templates for Plane D
```

This is what keeps D10 honest: the assistant has no built-in knowledge that any particular
project exists, so there is no coupling to any particular project.

---

## Memory

Four tiers, chosen by half-life. The single most important design decision for requirement 2a.

| Tier | Content | Mechanism | Half-life | Cost |
|---|---|---|---|---|
| **Live** | programs, services, scheduled tasks, disk, RAM/VRAM, processes, git state of every workspace | **tool calls against the machine** (D15) | seconds | ~0 tokens, ~0 VRAM |
| **Structural** | notes, `AGENTS.md`, `ARCHITECTURE.md`, schemas, manifests | `sqlite-vec` over the scoped note set; mtime-triggered re-index | hours | ~430 notes (D14) |
| **Episodic** | requests, decisions, failures, outcomes | append-only `journal.jsonl` / `decisions.jsonl` | permanent | disk |
| **Reflective** | friction log, capability memory, promoted constraints | Obsidian notes + `capabilities_memory.json` | permanent | ~500-token prompt overlay |

**The live tier is free, never goes stale, and needs no model.** It is also what makes "local
tech support" real: the assistant *checks* whether a service is running rather than recalling
that it probably is.

**RAG is used only for durable documents. Volatile state is never written into an index** — an
index of the system's current state is a lie with a timestamp on it.

### Obsidian bridge (D14)

`E:\Obsidian_Vault` is the substrate for the episodic and reflective tiers. Direct file I/O only —
no UI automation.

```
E:\Obsidian_Vault\
  Assistant\                 ← written by the daemon (its own state)
    journal\       journal.jsonl          episodic
    plans\         <slug>.md              decomposition + delegation records
    decisions\     decisions.jsonl        why, not just what
    capabilities\  <capability>.md        L1–L3 promoted capabilities
    reflection\    friction_log.md        Structural/Epistemic/Interaction/Strategy
  (read-only index scope)
    evernote\  .opencode\  recovery\  execution_plans\  copilot\  docs\  training\
  (hard-excluded)
    datasets\   .smart-env\
```

`.opencode/` in the vault is read as **role templates**, not as live configuration.

---

## The delegation loop (requirement 3)

```
  you ──▶ Plane B ──▶ brainstorm ──▶ plan
                                 (stages, agents, roles, security, QA, acceptance criteria)
                    │
                    ▼
              delegation graph persisted to plans\
                    │
                    ▼
              Plane C: assignTask → worker spawn → heartbeat/loop/breaker supervision
                    │
                    ▼
              auditors run as roles in the same graph — architect → worker → reviewer
                    │
                    ▼
              Plane B observes, escalates, reports. It does not implement. (D16)
```

A planner that also implements cannot supervise itself — its self-assessment collapses. Splitting
them is what makes supervision meaningful.

**Plane D is the only plane permitted to contact a cloud model provider (D11).** Planes A, B and
C never leave the device.

---

## Honest scope

Requirement 3 cannot be executed well by a local 9B (D17). The assistant plans, delegates, and
supervises; the delegated workers do the engineering. The assistant is the **always-on front
desk, personal automation layer, and orchestrator-in-chief** — not the chief engineer.

If that is not the shape you want, the honest answer is that the front desk + automation layer
portion (requirements 1, 2a, 2b, 2c, 2e) is entirely achievable on this hardware, and requirement
3 is achievable only as *orchestration of agents that are not this model*.

---

## Resource budget

| Component | RAM | VRAM | Lifetime |
|---|---|---|---|
| Plane A daemon | ~250–350 MB | 0 | always |
| Plane C supervisor (Node) | ~150–250 MB | 0 | always |
| Plane B brain (9B Q4_K_S) | ~1 GB | 5.2–5.5 GB | on demand, 90 s TTL |
| Plane B mmproj | ~0.4 GB | ~0.6 GB | only during vision |
| faster-whisper (CTranslate2) | ~1 GB | 0 (CPU) | only while listening |
| piper + openWakeWord | ~250 MB | 0 | always, tiny |
| **Worst case total** | **~4 GB** | **~6.1 GB** | |
| **Ceiling (D1)** | 16 GB | 7.0 GiB | |
| **Left for you** | **~28 GB** | **~5.9 GiB** | |

Plus one leased worker (Plane D) at a time, arbitrated. Figures are targets to be *measured* in
Step 1, not estimates.