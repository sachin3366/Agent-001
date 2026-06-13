# TDD Workflow Architecture

## Overview

An automated, state-machine-driven TDD loop that accepts a feature definition,
generates tests, verifies they fail, writes implementation, and loops until all
tests are green. The loop is driven by bash scripts; Claude acts as the code
generation engine at specific states.

---

## State Machine

```
                          ┌─────────────────────────────────────────────────────┐
                          │                                                     │
  feature.json            ▼                                                     │
  ──────────► [IDLE] ──► [PARSE_FEATURE] ──► [GENERATE_TESTS] ─────────────────┤
                                │                     │                         │
                                │ parse error         │ tests written           │
                                ▼                     ▼                         │
                            [ERROR]           [VERIFY_RED]                      │
                                                  │       │                     │
                               tests pass (bad!) │       │ tests fail (good)   │
                                                  ▼       ▼                     │
                                              [ERROR] [GENERATE_IMPL]           │
                                                           │                    │
                                                           │ code written       │
                                                           ▼                    │
                                                    [VERIFY_GREEN]              │
                                                      │         │               │
                                          all pass   │         │ still failing  │
                                                      ▼         │               │
                                               [REFACTOR]       │               │
                                                   │            │ retry count   │
                                       tests pass  │            │ exceeded      │
                                                   ▼            ▼               │
                                                [DONE]       [ERROR]            │
                                                                                │
                          (if REFACTOR breaks tests) ──────────────────────────┘
                          re-enters GENERATE_IMPL
```

### States

| State            | Description                                                          | Exit Conditions                                          |
|------------------|----------------------------------------------------------------------|----------------------------------------------------------|
| `IDLE`           | Waiting for a feature definition file                                | Feature file present → `PARSE_FEATURE`                  |
| `PARSE_FEATURE`  | Validate and load the feature JSON; resolve target paths             | Valid → `GENERATE_TESTS` / Invalid → `ERROR`            |
| `GENERATE_TESTS` | Call Claude to write a test file covering the feature contract       | File written → `VERIFY_RED`                             |
| `VERIFY_RED`     | Run the test suite; **must** fail here (Red phase)                   | Fails → `GENERATE_IMPL` / Passes → `ERROR`              |
| `GENERATE_IMPL`  | Call Claude to write/patch implementation until tests pass           | Code written → `VERIFY_GREEN`                           |
| `VERIFY_GREEN`   | Run the test suite; all tests must pass                              | All pass → `REFACTOR` / Failures remain → loop back     |
| `REFACTOR`       | Call Claude to clean up the implementation (no new logic)            | Code cleaned → `VERIFY_GREEN` (re-check)                |
| `DONE`           | All tests green; write a summary report                             | Terminal                                                 |
| `ERROR`          | Unrecoverable condition; log reason and exit non-zero               | Terminal                                                 |

---

## File & Directory Layout

```
Agent-001/
├── TDD_WORKFLOW.md          ← this document
│
├── .tdd/                    ← runtime workspace (gitignored)
│   ├── state.json           ← persisted state machine snapshot
│   ├── feature.json         ← active feature definition (copied in)
│   └── logs/
│       ├── run-<timestamp>.log
│       └── ...
│
├── scripts/
│   ├── tdd                  ← main entrypoint (chmod +x)
│   ├── states/
│   │   ├── parse_feature.sh
│   │   ├── generate_tests.sh
│   │   ├── verify_red.sh
│   │   ├── generate_impl.sh
│   │   ├── verify_green.sh
│   │   ├── refactor.sh
│   │   └── done.sh
│   └── lib/
│       ├── state.sh         ← read/write state.json helpers
│       ├── claude.sh        ← Claude API call wrapper
│       ├── runner.sh        ← test-runner abstraction (jest/pytest/go test)
│       └── log.sh           ← structured logging helpers
│
├── features/                ← feature definitions live here
│   └── example.json
│
└── src/                     ← production code (your project)
```

---

## Feature Definition Schema

A feature is described as a JSON file placed in `features/`. The workflow reads
this file as its primary input.

```jsonc
// features/example.json
{
  "id": "user-auth-login",           // unique slug (used for file naming)
  "description": "...",              // natural-language description for Claude
  "language": "typescript",          // typescript | python | go | javascript
  "framework": "jest",               // jest | pytest | go-test | vitest
  "test_style": "unit",              // unit | integration | e2e
  "target": {
    "src_path":  "src/auth/login.ts",   // where implementation goes
    "test_path": "src/auth/login.test.ts"
  },
  "contracts": [
    {
      "given": "valid credentials",
      "when":  "login() is called",
      "then":  "returns a signed JWT"
    },
    {
      "given": "wrong password",
      "when":  "login() is called",
      "then":  "throws AuthError with code INVALID_CREDENTIALS"
    }
  ],
  "constraints": [
    "no external HTTP calls in unit tests — mock the DB layer",
    "JWT must use HS256"
  ]
}
```

---

## State Persistence

`state.json` is the single source of truth for the runner. Every state
transition is written atomically so the loop can be safely re-entered after a
crash.

```jsonc
// .tdd/state.json
{
  "run_id":       "2026-06-12T16:30:00Z",
  "feature_id":   "user-auth-login",
  "current_state":"VERIFY_GREEN",
  "iteration":    2,            // how many GENERATE_IMPL → VERIFY_GREEN cycles
  "max_iterations": 5,          // hard ceiling before ERROR
  "test_output":  ".tdd/last_test_run.txt",
  "history": [
    { "state": "PARSE_FEATURE",   "ts": "...", "result": "ok" },
    { "state": "GENERATE_TESTS",  "ts": "...", "result": "ok" },
    { "state": "VERIFY_RED",      "ts": "...", "result": "failed (expected)" },
    { "state": "GENERATE_IMPL",   "ts": "...", "result": "ok", "iteration": 1 },
    { "state": "VERIFY_GREEN",    "ts": "...", "result": "2 failures remain" },
    { "state": "GENERATE_IMPL",   "ts": "...", "result": "ok", "iteration": 2 }
  ]
}
```

---

## Claude Integration Points

Claude is called at three states only. Each call is a structured prompt that
includes the feature definition and (where relevant) the failing test output.

### 1. GENERATE_TESTS prompt skeleton

```
You are a TDD engineer. Given the feature contract below, write ONLY the test
file. Do not write any implementation. The tests must fail with "not implemented"
or similar when run against an empty module.

Feature: <feature.json contents>
Test framework: <framework>
Test file path: <target.test_path>

Output the raw file contents, nothing else.
```

### 2. GENERATE_IMPL prompt skeleton

```
You are a TDD engineer in the GREEN phase. Fix the implementation so ALL tests
pass. Do not change the test file.

Test file: <test file contents>
Current implementation: <src file contents or "file does not exist yet">
Failing test output:
---
<last_test_run.txt contents>
---

Output the raw implementation file contents, nothing else. Iteration <n>/<max>.
```

### 3. REFACTOR prompt skeleton

```
You are a TDD engineer in the REFACTOR phase. All tests pass. Clean up the
implementation for readability and efficiency WITHOUT changing behavior. Do not
touch the test file.

Implementation: <src file contents>
Test file: <test file contents>

Output the raw refactored implementation, nothing else.
```

---

## Main Entrypoint: `scripts/tdd`

```
USAGE
  ./scripts/tdd run  <feature-file>   Start or resume a TDD loop
  ./scripts/tdd resume                Resume the loop from saved state
  ./scripts/tdd status                Print current state
  ./scripts/tdd reset                 Clear .tdd/ and start fresh

FLAGS
  --max-iterations N    Override default iteration cap (default: 5)
  --skip-refactor       Exit after VERIFY_GREEN without REFACTOR step
  --dry-run             Print prompts; do not call Claude or run tests
  --log-level debug|info|error
```

The entrypoint is a pure bash `while` loop over a `case` statement:

```bash
while true; do
  state=$(read_state)
  case "$state" in
    IDLE)            source states/parse_feature.sh  ;;
    PARSE_FEATURE)   source states/parse_feature.sh  ;;
    GENERATE_TESTS)  source states/generate_tests.sh ;;
    VERIFY_RED)      source states/verify_red.sh     ;;
    GENERATE_IMPL)   source states/generate_impl.sh  ;;
    VERIFY_GREEN)    source states/verify_green.sh   ;;
    REFACTOR)        source states/refactor.sh       ;;
    DONE)            source states/done.sh; break    ;;
    ERROR)           log_error; exit 1               ;;
    *)               log_error "unknown state: $state"; exit 1 ;;
  esac
done
```

Each state script **must** end by calling `write_state <NEXT_STATE>`.

---

## Test Runner Abstraction

`scripts/lib/runner.sh` exposes a single function:

```bash
run_tests <test_path>
# exits 0 if all pass, non-zero otherwise
# writes human-readable output to .tdd/last_test_run.txt
# writes machine-readable summary to .tdd/last_test_run.json
```

Internal dispatch (set once in feature.json, resolved at PARSE_FEATURE):

| framework   | command template                                         |
|-------------|----------------------------------------------------------|
| `jest`      | `npx jest --no-coverage --testPathPattern=<test_path>`   |
| `vitest`    | `npx vitest run <test_path>`                             |
| `pytest`    | `python -m pytest <test_path> -v`                        |
| `go-test`   | `go test ./... -run <test_filter> -v`                    |

---

## Loop Termination Conditions

| Condition                                         | Outcome     |
|---------------------------------------------------|-------------|
| All tests pass after VERIFY_GREEN                 | `DONE`      |
| VERIFY_RED shows tests unexpectedly passing       | `ERROR`     |
| `iteration > max_iterations`                      | `ERROR`     |
| REFACTOR causes test regressions                  | Back to `GENERATE_IMPL` (counts as iteration) |
| Claude returns malformed output (no code block)   | Retry once, then `ERROR` |
| Test runner not found / setup error               | `ERROR` (fatal, no retry) |

---

## Logging & Observability

Every state transition is logged to `.tdd/logs/run-<timestamp>.log` in a
structured format:

```
[2026-06-12T16:30:05Z] [INFO]  STATE_TRANSITION  VERIFY_RED → GENERATE_IMPL
[2026-06-12T16:30:05Z] [INFO]  TEST_FAILURES     count=3
[2026-06-12T16:30:08Z] [INFO]  CLAUDE_CALL       state=GENERATE_IMPL tokens_in=1842 tokens_out=312
[2026-06-12T16:30:08Z] [INFO]  FILE_WRITTEN      path=src/auth/login.ts bytes=487
```

`./scripts/tdd status` tails and formats this log live.

---

## Implementation Sequence

When we move from planning to code, build in this order so each piece is
testable before the next depends on it:

1. `scripts/lib/state.sh` — read/write state.json
2. `scripts/lib/log.sh` — structured logging
3. `scripts/lib/runner.sh` — test runner abstraction (mock mode first)
4. `scripts/lib/claude.sh` — Claude API wrapper (dry-run mode first)
5. `scripts/states/parse_feature.sh`
6. `scripts/states/generate_tests.sh`
7. `scripts/states/verify_red.sh`
8. `scripts/states/generate_impl.sh`
9. `scripts/states/verify_green.sh`
10. `scripts/states/refactor.sh`
11. `scripts/states/done.sh`
12. `scripts/tdd` — main entrypoint wiring all states

---

## Open Questions (resolve before coding)

1. **Claude API key** — passed as `ANTHROPIC_API_KEY` env var, or read from
   `.env`? Recommend env var only (never written to disk).

2. **Model choice** — `claude-sonnet-4-6` for speed in the tight loop, or
   `claude-opus-4-8` for quality? Could be a flag: `--model`.

3. **Output format** — should Claude return raw file content or a JSON envelope
   with `{ "path": "...", "content": "..." }`? JSON is safer to parse
   programmatically.

4. **Multi-file features** — v1 assumes one src file + one test file. Should
   the schema support multiple target files for larger features?

5. **Git integration** — auto-commit each GREEN iteration? Or leave git to the
   user?

6. **Parallel tests** — is the test runner expected to run the whole suite or
   only the feature's test file? Running only the feature file is faster but
   won't catch regressions.
