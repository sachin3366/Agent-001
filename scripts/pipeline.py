#!/usr/bin/env python3
"""
Research → Draft → Edit Pipeline — Phase 2B: Sequential pipeline pattern.

New concept vs pr_review.py (parallel fan-out):
  - Agents run ONE AT A TIME in a fixed order
  - Each agent's output is saved to disk and passed into the next agent's
    initial message — this is artifact hand-off
  - Each agent is stateless: it only knows what the pipeline hands it
  - The orchestrator is the only code that knows the full sequence

Artifact flow:
  topic (string)
    └─► [Research Agent] ──► workspace/findings.json
                                  │
                                  └─► [Writer Agent] ──► workspace/draft.md
                                                              │
                                                              └─► [Editor Agent] ──► workspace/final.md

Usage:
  python3 scripts/pipeline.py "how does garbage collection work in Python"
  python3 scripts/pipeline.py "explain JWT authentication" --verbose
  python3 scripts/pipeline.py "what is the CAP theorem" --out ./my-output
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
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
    print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env", file=sys.stderr)
    sys.exit(1)

import anthropic

# ── Configuration ─────────────────────────────────────────────────────────────

MODEL = "claude-sonnet-4-6"

def log(stage: str, msg: str, verbose: bool):
    if verbose:
        print(f"  [{stage:8s}] {msg}", file=sys.stderr)

# ── Generic Agent Runner ──────────────────────────────────────────────────────
#
# Same loop shape as pr_review.py and detective.py.
#
# One change: dispatch is now a CLOSURE passed in by the caller, not a
# method on a handler class. This works well here because each stage has
# only one tool (its terminal tool), so a full class would be overkill.
#
# The closure captures the stage's `result` dict and the `workspace` path,
# so the loop itself stays generic — it doesn't know what any tool does.

def run_agent(
    name: str,
    system_prompt: str,
    tools: list,
    dispatch,             # (tool_name: str, tool_input: dict) -> str
    initial_message: str,
    verbose: bool,
) -> None:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": initial_message}]
    log(name, "starting", verbose)

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            log(name, "stopped without calling terminal tool", verbose)
            break

        tool_results = []
        done = False

        for block in response.content:
            if block.type != "tool_use":
                continue
            log(name, f"→ {block.name}", verbose)
            result_text = dispatch(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            })
            if result_text == "__DONE__":
                log(name, "complete", verbose)
                done = True
                break

        messages.append({"role": "user", "content": tool_results})
        if done:
            break

# ── Stage 1: Research Agent ───────────────────────────────────────────────────
#
# No filesystem tools here. The research agent works entirely from its
# training knowledge and saves structured findings via its terminal tool.
# Keeping it tool-light shows that not every agent needs exploration tools.

RESEARCH_PROMPT = """You are a research agent. Research the given topic and produce structured findings.

Your output will be handed verbatim to a technical writer — write for an intermediate developer audience.

Requirements:
- Produce 6–10 specific, substantive findings (not vague generalities)
- Each finding must include a concrete detail, example, or number
- Confidence: high (well-established), medium (generally accepted), low (nuanced/debated)
- Key concepts: the 3–5 terms a reader must understand before the topic makes sense

Call save_findings once you have gathered enough to support a solid 500-word article."""

RESEARCH_TOOL = {
    "name": "save_findings",
    "description": "Save your research findings. Call once when research is complete.",
    "input_schema": {
        "type": "object",
        "properties": {
            "topic":   {"type": "string"},
            "summary": {"type": "string", "description": "2–3 sentence overview"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "point":      {"type": "string", "description": "A specific fact or concept"},
                        "detail":     {"type": "string", "description": "Explanation, example, or number"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["point", "detail", "confidence"],
                },
            },
            "key_concepts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3–5 must-know terms for this topic",
            },
        },
        "required": ["topic", "summary", "findings", "key_concepts"],
    },
}

def run_research(topic: str, workspace: Path, verbose: bool) -> dict:
    result = {}  # the closure writes into this dict

    def dispatch(tool_name: str, tool_input: dict) -> str:
        if tool_name == "save_findings":
            result.update(tool_input)
            path = workspace / "findings.json"
            path.write_text(json.dumps(tool_input, indent=2))
            log("research", f"saved → {path.name}", verbose)
            return "__DONE__"
        return f"ERROR: unknown tool '{tool_name}'"

    run_agent(
        name="research",
        system_prompt=RESEARCH_PROMPT,
        tools=[RESEARCH_TOOL],
        dispatch=dispatch,
        initial_message=f"Research this topic thoroughly: {topic}",
        verbose=verbose,
    )
    return result

# ── Stage 2: Writer Agent ─────────────────────────────────────────────────────
#
# The writer receives findings.json embedded in its initial message.
# It never calls list_directory or read_file — the pipeline already gave it
# everything it needs. This is the key characteristic of a pipeline agent:
# it is handed its inputs, not left to discover them.

WRITER_PROMPT = """You are a technical writer. You will receive structured research findings.
Write a clear, engaging technical explainer based ONLY on those findings.

Requirements:
- 400–600 words
- Target audience: intermediate developers
- Use ONLY facts present in the findings — do not add external knowledge
- Structure: brief intro → core explanation → concrete example → key takeaway
- Format in markdown: ## section headers, **bold** for key terms, code blocks where relevant

Call save_draft once the article is complete."""

WRITER_TOOL = {
    "name": "save_draft",
    "description": "Save the written draft. Call once when writing is complete.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title":      {"type": "string"},
            "content":    {"type": "string", "description": "Full markdown content of the article"},
            "word_count": {"type": "integer"},
        },
        "required": ["title", "content", "word_count"],
    },
}

def run_writer(topic: str, findings: dict, workspace: Path, verbose: bool) -> dict:
    result = {}

    def dispatch(tool_name: str, tool_input: dict) -> str:
        if tool_name == "save_draft":
            result.update(tool_input)
            path = workspace / "draft.md"
            path.write_text(tool_input["content"])
            log("writer", f"saved → {path.name} ({tool_input.get('word_count', '?')} words)", verbose)
            return "__DONE__"
        return f"ERROR: unknown tool '{tool_name}'"

    run_agent(
        name="writer",
        system_prompt=WRITER_PROMPT,
        tools=[WRITER_TOOL],
        dispatch=dispatch,
        # The findings are injected directly into the initial message.
        # This is artifact hand-off: the pipeline passes the previous
        # stage's output as the starting context for the next stage.
        initial_message=(
            f"Write a technical explainer on: {topic}\n\n"
            f"Research findings:\n```json\n{json.dumps(findings, indent=2)}\n```"
        ),
        verbose=verbose,
    )
    return result

# ── Stage 3: Editor Agent ─────────────────────────────────────────────────────
#
# The editor receives BOTH the draft AND the original findings.
# It uses findings as the ground truth for fact-checking — any claim in the
# draft not supported by the findings should be removed.
# This is a grounding constraint: the editor is bounded by the researcher's work.

EDITOR_PROMPT = """You are a technical editor. You will receive a draft article and the original research findings.

Your job in order:
1. Fact-check: remove any claim in the draft not supported by the research findings
2. Clarity: simplify complex sentences, replace jargon with plain language where it works
3. Tighten: cut filler phrases, redundant sentences, and anything that adds length but not value
4. Preserve: the author's structure, voice, and all accurate technical content

Track every significant change. List removed claims separately (even if the list is empty).

Call save_final with the polished article."""

EDITOR_TOOL = {
    "name": "save_final",
    "description": "Save the final edited article. Call once when editing is complete.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title":   {"type": "string"},
            "content": {"type": "string", "description": "Final polished markdown"},
            "changes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Each significant edit made",
            },
            "removed_claims": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Claims removed because they were not in the research findings",
            },
        },
        "required": ["title", "content", "changes", "removed_claims"],
    },
}

def run_editor(topic: str, findings: dict, draft: dict, workspace: Path, verbose: bool) -> dict:
    result = {}

    def dispatch(tool_name: str, tool_input: dict) -> str:
        if tool_name == "save_final":
            result.update(tool_input)
            path = workspace / "final.md"
            path.write_text(tool_input["content"])
            log("editor", f"saved → {path.name}", verbose)
            return "__DONE__"
        return f"ERROR: unknown tool '{tool_name}'"

    run_agent(
        name="editor",
        system_prompt=EDITOR_PROMPT,
        tools=[EDITOR_TOOL],
        dispatch=dispatch,
        initial_message=(
            f"Edit this draft about: {topic}\n\n"
            f"## Original Research (use as ground truth for fact-checking)\n"
            f"```json\n{json.dumps(findings, indent=2)}\n```\n\n"
            f"## Draft to Edit\n\n{draft.get('content', '')}"
        ),
        verbose=verbose,
    )
    return result

# ── Pipeline Orchestrator ─────────────────────────────────────────────────────
#
# Compare this to run_review() in pr_review.py:
#
#   pr_review (parallel):
#     futures = {executor.submit(agent) for agent in agents}  ← all start at once
#     for future in as_completed(futures): collect result     ← finish in any order
#
#   pipeline (sequential):
#     findings = run_research(...)                            ← must finish first
#     draft    = run_writer(..., findings)                    ← needs findings
#     final    = run_editor(..., findings, draft)             ← needs both
#
# Sequential BECAUSE each stage depends on the previous stage's output.
# Parallel BECAUSE each stage is independent (pr_review agents don't share data).
# The data dependency is what forces the sequence.

def run_pipeline(topic: str, workspace: Path, verbose: bool) -> dict:
    print(f"\n[pipeline] topic:     {topic}", file=sys.stderr)
    print(f"[pipeline] workspace: {workspace}\n", file=sys.stderr)

    print("[pipeline] 1/3 researching...", file=sys.stderr)
    findings = run_research(topic, workspace, verbose)
    if not findings.get("findings"):
        print("ERROR: research stage produced no findings", file=sys.stderr)
        sys.exit(1)

    print("[pipeline] 2/3 writing draft...", file=sys.stderr)
    draft = run_writer(topic, findings, workspace, verbose)
    if not draft.get("content"):
        print("ERROR: writer stage produced no content", file=sys.stderr)
        sys.exit(1)

    print("[pipeline] 3/3 editing...", file=sys.stderr)
    final = run_editor(topic, findings, draft, workspace, verbose)
    if not final.get("content"):
        print("ERROR: editor stage produced no content", file=sys.stderr)
        sys.exit(1)

    return {
        "topic":     topic,
        "workspace": str(workspace),
        "artifacts": {
            "findings": str(workspace / "findings.json"),
            "draft":    str(workspace / "draft.md"),
            "final":    str(workspace / "final.md"),
        },
        "stats": {
            "findings_count":  len(findings.get("findings", [])),
            "key_concepts":    findings.get("key_concepts", []),
            "draft_words":     draft.get("word_count", 0),
            "edits_made":      len(final.get("changes", [])),
            "claims_removed":  len(final.get("removed_claims", [])),
        },
    }

# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Research → Draft → Edit pipeline")
    parser.add_argument("topic", nargs="+", help="Topic to research and write about")
    parser.add_argument("--out", default="./output", metavar="DIR",
                        help="Output directory (default: ./output)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show each agent's tool calls")
    args = parser.parse_args()

    topic = " ".join(args.topic)
    out_dir = Path(args.out)

    # Workspace: ./output/<topic-slug>-<time>/
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40]
    ts = datetime.now().strftime("%H%M%S")
    workspace = out_dir / f"{slug}-{ts}"
    workspace.mkdir(parents=True, exist_ok=True)

    summary = run_pipeline(topic, workspace, args.verbose)

    print("\n[pipeline] done.\n", file=sys.stderr)
    print(f"Artifacts written to: {workspace}\n")
    print(f"  findings.json  — {summary['stats']['findings_count']} findings, "
          f"key concepts: {', '.join(summary['stats']['key_concepts'])}")
    print(f"  draft.md       — {summary['stats']['draft_words']} words")
    print(f"  final.md       — {summary['stats']['edits_made']} edits made, "
          f"{summary['stats']['claims_removed']} claims removed")
    print(f"\nFinal article: {workspace / 'final.md'}")

if __name__ == "__main__":
    main()
