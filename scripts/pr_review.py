#!/usr/bin/env python3
"""
PR Reviewer with Specialists — Phase 2: Orchestrator + Worker pattern.

New concepts vs Phase 1 (detective.py):
  - Orchestrator spawns 3 specialist agents
  - All 3 run in PARALLEL (ThreadPoolExecutor)
  - Each specialist has its own isolated state (SpecialistHandler)
  - Orchestrator aggregates heterogeneous results into one report

Usage:
  python3 scripts/pr_review.py                   # reviews last commit
  python3 scripts/pr_review.py --staged          # reviews staged changes
  python3 scripts/pr_review.py --diff path.diff  # reviews a diff file
  python3 scripts/pr_review.py --json            # output raw JSON
  python3 scripts/pr_review.py --verbose         # show each agent's tool calls
"""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── Bootstrap ─────────────────────────────────────────────────────────────────

_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

if not os.environ.get("ANTHROPIC_API_KEY"):
    print("ERROR: ANTHROPIC_API_KEY not set. See scripts/detective.py for setup.", file=sys.stderr)
    sys.exit(1)

import anthropic

# ── Configuration ─────────────────────────────────────────────────────────────

MODEL = "claude-sonnet-4-6"
FILE_READ_BUDGET = 10  # per specialist — they're focused, need fewer reads than detective

# Parallel agents write to stderr simultaneously.
# This lock ensures their lines don't interleave mid-write.
_print_lock = threading.Lock()

def log(agent: str, msg: str, verbose: bool):
    if not verbose:
        return
    with _print_lock:
        print(f"  [{agent:8s}] {msg}", file=sys.stderr)

# ── Tools ─────────────────────────────────────────────────────────────────────
#
# Every specialist gets the same read/list tools.
# What differs between agents is the system prompt — not the toolset.
# This is a key design principle: tools define CAPABILITY, prompts define BEHAVIOUR.

TOOLS = [
    {
        "name": "list_directory",
        "description": "List directory contents. Free — does not count against read budget.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a source file for context. Costs 1 from your 10-read budget.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "submit_findings",
        "description": "Submit your findings and end the review. Call this exactly once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "severity":   {"type": "string", "enum": ["high", "medium", "low", "info"]},
                            "title":      {"type": "string"},
                            "location":   {"type": "string", "description": "filename:line or 'general'"},
                            "detail":     {"type": "string", "description": "What the problem is and why it matters"},
                            "suggestion": {"type": "string", "description": "Concrete, actionable fix"},
                        },
                        "required": ["severity", "title", "location", "detail", "suggestion"],
                    },
                },
                "summary":    {"type": "string", "description": "One-sentence overall verdict"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["findings", "summary", "confidence"],
        },
    },
]

# ── Tool Handler ──────────────────────────────────────────────────────────────
#
# One SpecialistHandler instance per agent.
# Because each agent runs in its own thread with its own handler,
# there is NO shared mutable state between agents — thread-safe by design.

class SpecialistHandler:
    def __init__(self, agent_name: str, repo_root: Path, verbose: bool):
        self.agent_name = agent_name
        self.root = repo_root
        self.reads_used = 0
        self.verbose = verbose
        self.result = None  # populated by submit_findings

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        return (self.root / p).resolve() if not p.is_absolute() else p.resolve()

    def list_directory(self, path: str) -> str:
        p = self._resolve(path)
        if not p.is_dir():
            return f"ERROR: not a directory: {path}"
        try:
            lines = []
            for child in sorted(p.iterdir()):
                kind = "dir " if child.is_dir() else "file"
                size = f"  {child.stat().st_size:>8} bytes" if child.is_file() else ""
                lines.append(f"  {kind}  {child.name}{size}")
            return "\n".join(lines) if lines else "(empty)"
        except PermissionError:
            return f"ERROR: permission denied: {path}"

    def read_file(self, path: str) -> str:
        if self.reads_used >= FILE_READ_BUDGET:
            return "Budget exhausted. Call submit_findings now."
        p = self._resolve(path)
        if not p.is_file():
            return f"ERROR: not a file: {path}"
        self.reads_used += 1
        remaining = FILE_READ_BUDGET - self.reads_used
        log(self.agent_name, f"read {self.reads_used}/{FILE_READ_BUDGET}: {path}", self.verbose)
        try:
            content = p.read_text(errors="replace")
            if len(content) > 6000:
                content = content[:6000] + "\n... [truncated]"
        except Exception as e:
            return f"ERROR: {e}"
        return content + f"\n\n[Budget: {remaining} reads remaining]"

    def submit_findings(self, findings: list, summary: str, confidence: str) -> str:
        self.result = {
            "agent":      self.agent_name,
            "findings":   findings,
            "summary":    summary,
            "confidence": confidence,
        }
        return "__DONE__"

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "list_directory":
            return self.list_directory(tool_input["path"])
        elif tool_name == "read_file":
            return self.read_file(tool_input["path"])
        elif tool_name == "submit_findings":
            return self.submit_findings(
                tool_input["findings"],
                tool_input["summary"],
                tool_input["confidence"],
            )
        return f"ERROR: unknown tool '{tool_name}'"

# ── Specialist System Prompts ─────────────────────────────────────────────────
#
# This is where specialisation lives — not in different tool sets or different
# code, but in different instructions given to the same underlying model.
# All three agents are identical loops; only their goals differ.

SECURITY_PROMPT = """You are a security code reviewer. Your job is to find security issues in the given git diff.

Focus on:
- Injection vulnerabilities (SQL, shell command, path traversal)
- Hardcoded secrets, API keys, tokens, or passwords
- Insecure use of eval, exec, or dynamic code execution
- Missing input validation at system boundaries (user input, file paths, API params)
- Broken authentication or authorisation logic
- Unsafe deserialization

Strategy:
1. Read the diff carefully — it is your primary source.
2. Use read_file only when you see a suspicious pattern and need surrounding context.
3. Report real, exploitable issues. Do not flag theoretical or stylistic concerns.
4. If there are zero issues, submit an empty findings list — do not invent problems.
5. Call submit_findings when done."""

COVERAGE_PROMPT = """You are a test coverage reviewer. Your job is to find untested code in the given git diff.

Focus on:
- New functions or methods that have no corresponding test
- Changed logic paths not exercised by existing tests
- Missing edge case tests (null/None, empty collections, boundary values)
- Missing error path tests (what happens when things fail)
- Tests that only cover the happy path

Strategy:
1. Read the diff to identify all changed or added functions/methods.
2. Use list_directory and read_file to locate test files for the changed modules.
3. Compare what the tests cover vs what the production code does.
4. Order findings by impact — untested critical paths before minor gaps.
5. Call submit_findings when done."""

STYLE_PROMPT = """You are a code quality reviewer. Your job is to find maintainability issues in the given git diff.

Focus on:
- Poorly named variables, functions, or classes (name doesn't match behaviour)
- Functions doing too many things (violates single responsibility)
- Duplicated logic that should be extracted into a shared helper
- Excessive complexity: deep nesting, very long functions, unclear conditionals
- Dead code or unused imports introduced in this diff

Strategy:
1. Read the diff — it is your primary source. Read files only for extra context.
2. Only flag objectively problematic issues, not personal style preference.
3. For every finding, give a concrete rename or refactor as the suggestion.
4. Call submit_findings when done."""

SPECIALISTS = [
    ("security", SECURITY_PROMPT),
    ("coverage", COVERAGE_PROMPT),
    ("style",    STYLE_PROMPT),
]

# ── Generic Agent Runner ──────────────────────────────────────────────────────
#
# This is the same agentic loop as detective.py, extracted into a reusable
# function. Notice what is parameterised:
#   - system_prompt  → changes the agent's domain
#   - handler        → isolated state, one per agent
# Everything else (the loop shape, tool dispatch, termination) is identical.

def run_specialist(
    agent_name: str,
    system_prompt: str,
    diff: str,
    repo_root: Path,
    verbose: bool,
) -> dict:
    client = anthropic.Anthropic()
    handler = SpecialistHandler(agent_name, repo_root, verbose)

    messages = [
        {
            "role": "user",
            "content": (
                f"Review this diff for issues in your domain. "
                f"Repository root: {repo_root}\n\n"
                f"```diff\n{diff}\n```"
            ),
        }
    ]

    log(agent_name, "starting", verbose)

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            log(agent_name, "stopped without submit_findings", verbose)
            break

        tool_results = []
        done = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            log(agent_name, f"→ {block.name}({json.dumps(block.input)[:80]})", verbose)
            result = handler.dispatch(block.name, block.input)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

            if result == "__DONE__":
                log(agent_name, "complete", verbose)
                done = True
                break

        messages.append({"role": "user", "content": tool_results})
        if done:
            break

    return handler.result or {
        "agent": agent_name, "findings": [],
        "summary": "no result returned", "confidence": "low",
    }

# ── Orchestrator ──────────────────────────────────────────────────────────────
#
# The orchestrator's only job: launch workers and collect results.
# It does not review code itself — it delegates everything.
#
# ThreadPoolExecutor(max_workers=3) starts all three agents simultaneously.
# as_completed() yields each future as it finishes, so fast agents don't
# wait for slow ones before their result is processed.

def run_review(diff: str, repo_root: Path, verbose: bool) -> dict:
    if verbose:
        print(
            f"\n[orchestrator] launching {len(SPECIALISTS)} specialists in parallel\n",
            file=sys.stderr,
        )

    agent_results = {}

    with ThreadPoolExecutor(max_workers=len(SPECIALISTS)) as executor:
        futures = {
            executor.submit(run_specialist, name, prompt, diff, repo_root, verbose): name
            for name, prompt in SPECIALISTS
        }

        for future in as_completed(futures):
            name = futures[future]
            try:
                agent_results[name] = future.result()
            except Exception as e:
                agent_results[name] = {
                    "agent": name, "findings": [],
                    "summary": f"agent crashed: {e}", "confidence": "low",
                }
            log(name, "result received by orchestrator", verbose)

    return aggregate(agent_results)

# ── Aggregator ────────────────────────────────────────────────────────────────

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

def aggregate(agent_results: dict) -> dict:
    all_findings = []
    summaries = {}

    for name, result in agent_results.items():
        summaries[name] = {
            "summary":    result.get("summary", ""),
            "confidence": result.get("confidence", "low"),
            "count":      len(result.get("findings", [])),
        }
        for f in result.get("findings", []):
            f["agent"] = name
            all_findings.append(f)

    all_findings.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity", "info"), 99))

    counts = {s: 0 for s in SEVERITY_ORDER}
    for f in all_findings:
        counts[f.get("severity", "info")] += 1

    return {
        "findings":        all_findings,
        "agent_summaries": summaries,
        "counts":          counts,
        "total":           len(all_findings),
    }

# ── Markdown Formatter ────────────────────────────────────────────────────────

AGENT_EMOJI = {"security": "🔒", "coverage": "🧪", "style": "✏️"}

def format_markdown(report: dict) -> str:
    c = report["counts"]
    lines = [
        "# PR Review Report",
        "",
        f"**{report['total']} finding(s):** "
        f"{c['high']} high · {c['medium']} medium · {c['low']} low · {c['info']} info",
        "",
        "## Agent Summaries",
        "",
    ]

    for name, s in report["agent_summaries"].items():
        emoji = AGENT_EMOJI.get(name, "•")
        lines.append(
            f"- **{emoji} {name.capitalize()}** "
            f"({s['count']} findings, {s['confidence']} confidence): {s['summary']}"
        )
    lines.append("")

    if not report["findings"]:
        lines.append("No findings. LGTM. ✅")
        return "\n".join(lines)

    for severity in ["high", "medium", "low", "info"]:
        section = [f for f in report["findings"] if f.get("severity") == severity]
        if not section:
            continue
        lines += [f"## {severity.capitalize()}", ""]
        for f in section:
            lines += [
                f"### [{f['agent']}] {f['title']}",
                f"**Location:** `{f['location']}`  ",
                "",
                f"{f['detail']}",
                "",
                f"**Suggestion:** {f['suggestion']}",
                "",
                "---",
                "",
            ]

    return "\n".join(lines)

# ── Diff Collection ───────────────────────────────────────────────────────────

def get_diff(args: list) -> str:
    if "--diff" in args:
        idx = args.index("--diff")
        path = Path(args[idx + 1])
        if not path.exists():
            print(f"ERROR: diff file not found: {path}", file=sys.stderr)
            sys.exit(1)
        return path.read_text()

    if "--staged" in args:
        cmd = ["git", "diff", "--cached"]
    else:
        cmd = ["git", "diff", "HEAD~1", "HEAD"]

    result = subprocess.run(cmd, capture_output=True, text=True)

    # Fallback: single-commit repo has no HEAD~1
    if not result.stdout.strip():
        result = subprocess.run(["git", "show", "HEAD"], capture_output=True, text=True)

    if not result.stdout.strip():
        print("ERROR: no diff found. Use --diff <file> to supply one.", file=sys.stderr)
        sys.exit(1)

    return result.stdout

# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    verbose = "--verbose" in args or "-v" in args
    as_json = "--json" in args

    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    repo_root = Path(result.stdout.strip()) if result.returncode == 0 else Path(".")

    diff = get_diff(args)

    if verbose:
        print(f"[orchestrator] repo:      {repo_root}", file=sys.stderr)
        print(f"[orchestrator] diff size: {len(diff)} chars", file=sys.stderr)

    report = run_review(diff, repo_root, verbose)

    print(json.dumps(report, indent=2) if as_json else format_markdown(report))

if __name__ == "__main__":
    main()
