# Agent-001 — Learning Agent Workflow Development

A progressive, hands-on repository for learning how to build AI agents and
multi-agent systems using the Anthropic Claude API and Claude Code.

Each phase introduces new orchestration concepts. Code is written first, then
explained — so the repo itself is the textbook.

---

## Phases

### Phase 1 — Single Agent Fundamentals ✅
> *Core concept: tool-use loop, budget enforcement, structured output*

**Project: File System Detective** ([`scripts/detective.py`](scripts/detective.py))

An agent that analyses any software project directory and produces a structured
JSON health report — without reading more than 20 files.

Key patterns introduced:
- The agentic `while` loop: ask → execute tools → feed results back → repeat
- Budget enforcement in the tool handler (not in the prompt)
- Terminal tool pattern: `submit_report` forces schema-validated output
- Injecting runtime state into tool results so the model adapts mid-task

**TDD Workflow** ([`TDD_WORKFLOW.md`](TDD_WORKFLOW.md))

State-machine architecture for an automated Test-Driven Development loop.
Accepts a feature definition, generates tests, verifies failure, writes
implementation, and loops until zero errors. Planned but not yet implemented —
serves as the architecture reference for Phase 3.

---

### Phase 2 — Orchestrator + Worker Pattern ✅
> *Core concept: fan-out, parallel execution, result aggregation*

**Project: PR Reviewer with Specialists** ([`scripts/pr_review.py`](scripts/pr_review.py))

An orchestrator that spawns three specialist agents in parallel — Security,
Coverage, and Style — each running its own independent agentic loop. Results
are aggregated into a single markdown report sorted by severity.

Key patterns introduced:
- `ThreadPoolExecutor` fan-out: all agents start simultaneously, not sequentially
- `as_completed()`: process each result as it arrives, not in submission order
- One handler instance per agent: thread-safe by isolation, not by locking
- Shared toolset, different system prompts: capability vs behaviour separation
- Generic `run_specialist()` loop: Phase 1's loop extracted and parameterised

Planned (next):
- **Research → Draft → Edit Pipeline** — three sequential agents passing
  artifacts to each other via a shared workspace

---

### Phase 3 — State Machine Workflows *(coming)*
> *Core concept: long-horizon tasks, checkpointing, veto patterns*

Planned projects:
- Implement the TDD workflow designed in Phase 1
- Multi-stage code review with veto gates
- Automated bug triage (reproduce → bisect → assign → notify)

---

### Phase 4 — Adversarial & Self-Improving Agents *(coming)*
> *Core concept: red-team/blue-team loops, meta-agents, convergence detection*

Planned projects:
- Red-team / Blue-team code hardener
- Self-improving prompt engineer (agent that rewrites its own prompts)

---

## Project Structure

```
Agent-001/
├── README.md               ← this file (grows with each phase)
├── TDD_WORKFLOW.md         ← state-machine design doc for Phase 3
├── .gitignore
└── scripts/
    ├── detective.py        ← Phase 1: File System Detective agent
    └── pr_review.py        ← Phase 2: PR Reviewer with Specialists
```

---

## Setup

```bash
# Clone
git clone git@github.com:sachin3366/Agent-001.git
cd Agent-001

# Set your Anthropic API key (get one at console.anthropic.com)
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env

# Run the detective agent on any project
python3 scripts/detective.py /path/to/some/project --verbose
```

**Requirements:** Python 3.10+, `anthropic` SDK (`pip install anthropic`)

---

## Concepts Covered So Far

| Concept | Where |
|---|---|
| Agentic tool-use loop | `detective.py` lines 272–326 |
| Tool budget enforcement | `ToolHandler.read_file` |
| Structured output via terminal tool | `submit_report` schema |
| System prompt as reasoning scaffold | `SYSTEM_PROMPT` constant |
| Runtime state injection into tool results | budget footer in `read_file` |
| State machine design | `TDD_WORKFLOW.md` |
| Parallel agent fan-out | `pr_review.py` — `run_review()` |
| `as_completed()` result collection | `pr_review.py` — orchestrator loop |
| Thread-safe isolation via separate instances | `SpecialistHandler` per agent |
| Capability vs behaviour separation | shared `TOOLS`, different prompts |
| Generic reusable agent loop | `run_specialist()` function |

---

## Learning Path

Start here if you're new to agent development:

1. Read [`TDD_WORKFLOW.md`](TDD_WORKFLOW.md) for the state machine mental model
2. Read through [`scripts/detective.py`](scripts/detective.py) section by section
3. Run it with `--verbose` on a real project and watch the tool calls
4. Experiment: lower `FILE_READ_BUDGET` to 5 and see how the agent's strategy changes
5. Add a `grep_file` tool and observe how the agent starts preferring it
