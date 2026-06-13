#!/usr/bin/env python3
"""
File System Detective — Tier 1 agent learning project.

Demonstrates:
  - Agentic tool-use loop (model drives its own exploration)
  - Hard budget enforcement (20 file reads, tracked in handler state)
  - Structured output via a terminal "submit_report" tool
  - Graceful degradation when budget is exhausted

Usage:
  python3 scripts/detective.py <directory>
  python3 scripts/detective.py <directory> --verbose
"""

import json
import os
import sys
from pathlib import Path

# Load .env from repo root before importing anthropic
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

if not os.environ.get("ANTHROPIC_API_KEY"):
    print(
        "ERROR: ANTHROPIC_API_KEY is not set.\n"
        "  Option 1 — export it in your shell:\n"
        "      export ANTHROPIC_API_KEY=sk-ant-...\n"
        "  Option 2 — create a .env file in the repo root:\n"
        "      echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env\n"
        "  Get a key at: https://console.anthropic.com/",
        file=sys.stderr,
    )
    sys.exit(1)

import anthropic

# ── Configuration ─────────────────────────────────────────────────────────────

MODEL = "claude-sonnet-4-6"
FILE_READ_BUDGET = 20

SYSTEM_PROMPT = """You are a File System Detective. Your job is to analyse a software project directory
and produce an accurate, actionable report — without reading more than {budget} files.

Strategy:
1. Start with list_directory on the root. Read the tree to understand structure.
2. Prioritise high-signal files first: package.json, pyproject.toml, go.mod, Cargo.toml,
   README, Makefile, .github/workflows/, Dockerfile, *test* files.
3. Infer as much as possible from filenames and directory names alone.
4. Only read a file when its contents would meaningfully change your conclusion.
5. When confident (or budget is nearly exhausted), call submit_report.

The submit_report tool is the ONLY way to output your findings. Do not print JSON yourself.
""".format(budget=FILE_READ_BUDGET)

# ── Tool Definitions ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "list_directory",
        "description": (
            "List the contents of a directory. Returns file names, types (file/dir), "
            "and sizes in bytes. Does NOT count against the file-read budget."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to list (absolute or relative to target dir)"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the full text content of a file. "
            "Costs 1 read from your budget. "
            "Current budget status is shown in the tool result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "submit_report",
        "description": (
            "Submit the final analysis report. Call this when you have enough information. "
            "This ends the analysis — you cannot use other tools after calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report": {
                    "type": "object",
                    "description": "Structured project report",
                    "properties": {
                        "project_name":     {"type": "string"},
                        "description":      {"type": "string", "description": "1-3 sentence summary of what this project does"},
                        "primary_language": {"type": "string"},
                        "frameworks":       {"type": "array", "items": {"type": "string"}},
                        "audience": {
                            "type": "object",
                            "properties": {
                                "primary":         {"type": "string", "description": "Who this is built for"},
                                "technical_level": {"type": "string", "enum": ["beginner", "intermediate", "expert"]},
                                "reasoning":       {"type": "string"},
                            },
                            "required": ["primary", "technical_level", "reasoning"],
                        },
                        "health": {
                            "type": "object",
                            "properties": {
                                "has_tests":            {"type": "boolean"},
                                "has_readme":           {"type": "boolean"},
                                "has_ci":               {"type": "boolean"},
                                "has_linting":          {"type": "boolean"},
                                "has_dependency_lock":  {"type": "boolean"},
                                "has_dockerfile":       {"type": "boolean"},
                            },
                            "required": ["has_tests", "has_readme", "has_ci", "has_linting", "has_dependency_lock"],
                        },
                        "missing": {
                            "type": "array",
                            "description": "Ordered list of gaps, most important first",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "item":       {"type": "string"},
                                    "severity":   {"type": "string", "enum": ["high", "medium", "low"]},
                                    "suggestion": {"type": "string", "description": "Concrete next step to address this gap"},
                                },
                                "required": ["item", "severity", "suggestion"],
                            },
                        },
                        "files_read":  {"type": "integer", "description": "How many files were read (not listed)"},
                        "confidence":  {"type": "string", "enum": ["high", "medium", "low"],
                                        "description": "Confidence in the overall report given budget constraints"},
                    },
                    "required": ["project_name", "description", "primary_language", "audience", "health", "missing", "files_read", "confidence"],
                }
            },
            "required": ["report"],
        },
    },
]

# ── Tool Handlers ──────────────────────────────────────────────────────────────

class BudgetExhausted(Exception):
    pass

class ToolHandler:
    def __init__(self, root: Path, budget: int, verbose: bool):
        self.root = root
        self.budget = budget
        self.reads_used = 0
        self.verbose = verbose
        self.report = None          # set when submit_report is called

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        return p.resolve()

    def list_directory(self, path: str) -> str:
        p = self._resolve(path)
        if not p.exists():
            return f"ERROR: {path} does not exist"
        if not p.is_dir():
            return f"ERROR: {path} is not a directory"

        entries = []
        try:
            for child in sorted(p.iterdir()):
                kind = "dir " if child.is_dir() else "file"
                size = ""
                if child.is_file():
                    try:
                        size = f"  {child.stat().st_size:>8} bytes"
                    except OSError:
                        size = "  [unreadable]"
                entries.append(f"  {kind}  {child.name}{size}")
        except PermissionError:
            return f"ERROR: permission denied for {path}"

        if not entries:
            return "(empty directory)"
        return "\n".join(entries)

    def read_file(self, path: str) -> str:
        remaining = self.budget - self.reads_used
        if remaining <= 0:
            raise BudgetExhausted("File-read budget exhausted. Call submit_report now.")

        p = self._resolve(path)
        if not p.exists():
            return f"ERROR: {path} does not exist"
        if not p.is_file():
            return f"ERROR: {path} is not a regular file"

        self.reads_used += 1
        remaining_after = self.budget - self.reads_used

        if self.verbose:
            print(f"  [read {self.reads_used}/{self.budget}] {path}", file=sys.stderr)

        try:
            content = p.read_text(errors="replace")
            # cap at 8 KB to keep context manageable
            if len(content) > 8000:
                content = content[:8000] + f"\n... [truncated — {len(content)} bytes total]"
        except Exception as e:
            content = f"ERROR reading file: {e}"

        footer = f"\n\n[Budget: {remaining_after} reads remaining]"
        if remaining_after <= 3:
            footer += " ⚠️  Consider calling submit_report soon."
        return content + footer

    def submit_report(self, report: dict) -> str:
        report["files_read"] = self.reads_used
        self.report = report
        return "__DONE__"

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "list_directory":
            return self.list_directory(tool_input["path"])
        elif tool_name == "read_file":
            return self.read_file(tool_input["path"])
        elif tool_name == "submit_report":
            return self.submit_report(tool_input["report"])
        else:
            return f"ERROR: unknown tool '{tool_name}'"

# ── Agent Loop ─────────────────────────────────────────────────────────────────

def run_detective(target_dir: str, verbose: bool = False) -> dict:
    root = Path(target_dir).resolve()
    if not root.exists() or not root.is_dir():
        print(f"ERROR: '{target_dir}' is not a valid directory", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic()
    handler = ToolHandler(root, FILE_READ_BUDGET, verbose)

    messages = [
        {
            "role": "user",
            "content": f"Analyse this project directory and submit your report.\n\nTarget directory: {root}",
        }
    ]

    iteration = 0

    if verbose:
        print(f"\n[detective] target: {root}", file=sys.stderr)
        print(f"[detective] model:  {MODEL}", file=sys.stderr)
        print(f"[detective] budget: {FILE_READ_BUDGET} file reads\n", file=sys.stderr)

    while True:
        iteration += 1

        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if verbose:
            print(f"[loop {iteration}] stop_reason={response.stop_reason}  "
                  f"tool_calls={sum(1 for b in response.content if b.type == 'tool_use')}",
                  file=sys.stderr)

        # Add assistant turn to history
        messages.append({"role": "assistant", "content": response.content})

        # If model is done (no tool calls), break
        if response.stop_reason == "end_turn":
            if handler.report is None:
                print("WARNING: model stopped without calling submit_report", file=sys.stderr)
            break

        # Process tool calls
        tool_results = []
        done = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            if verbose:
                print(f"  → {block.name}({json.dumps(block.input)[:120]})", file=sys.stderr)

            try:
                result_text = handler.dispatch(block.name, block.input)
            except BudgetExhausted as e:
                result_text = str(e)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            })

            if result_text == "__DONE__":
                done = True
                break

        messages.append({"role": "user", "content": tool_results})

        if done:
            break

    return handler.report or {"error": "no report submitted"}

# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/detective.py <directory> [--verbose]")
        sys.exit(1)

    target = sys.argv[1]
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    report = run_detective(target, verbose=verbose)
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
