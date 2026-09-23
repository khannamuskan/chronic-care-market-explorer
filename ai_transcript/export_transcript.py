"""Export the Copilot CLI session log into a readable Markdown transcript.

The assignment asks for the full AI conversation. Rather than paste screenshots
or hand-curate a summary, this script renders the raw session event log
(`events.jsonl`) that the CLI writes, so the transcript is verifiably complete
and can be regenerated at any time.

Usage
-----
    python ai_transcript/export_transcript.py <path-to-events.jsonl>

    # or let it find the newest session automatically
    python ai_transcript/export_transcript.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

OUT_PATH = Path(__file__).parent / "transcript.md"
# Tool results can be very long (full API payloads); truncate for readability.
MAX_RESULT_CHARS = 2_000
MAX_ARG_CHARS = 2_500


def find_latest_session() -> Path:
    root = Path.home() / ".copilot" / "session-state"
    candidates = sorted(
        root.glob("*/events.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise SystemExit(f"No session logs found under {root}")
    return candidates[0]


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit:,} more characters]"


def fmt_time(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except ValueError:
        return ts


def unwrap_result(result) -> str:
    """Pull the human-readable text out of a tool result payload.

    Results arrive either as a plain string or as a dict/list wrapper around a
    ``content`` field; rendering the raw JSON would fill the transcript with
    escaped newlines and make it unreadable.
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        for key in ("content", "text", "output", "stdout"):
            if key in result:
                return unwrap_result(result[key])
        return json.dumps(result, indent=2)
    if isinstance(result, list):
        return "\n".join(filter(None, (unwrap_result(r) for r in result)))
    return str(result).strip()


def render(events_path: Path) -> str:
    events = []
    with events_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # tool.execution_complete carries only a toolCallId, so build a lookup of
    # call id -> (tool name, intent) from the matching start events.
    call_index: dict[str, tuple[str, str]] = {}
    for ev in events:
        if ev.get("type") == "tool.execution_start":
            d = ev.get("data", {}) or {}
            args = d.get("arguments", {}) or {}
            call_index[d.get("toolCallId", "")] = (
                d.get("toolName", "tool"), args.get("description", "")
            )

    out: list[str] = [
        "# AI Conversation Transcript",
        "",
        "Full, unedited conversation for the **Chronic Care Market Prioritization",
        "Explorer** assignment. Rendered directly from the GitHub Copilot CLI",
        "session event log by `export_transcript.py`, nothing has been curated",
        "or removed. Tool outputs are truncated only where noted.",
        "",
        "- **Assistant model:** Claude Opus 5 (GitHub Copilot CLI)",
        f"- **Session log:** `{events_path.name}`",
        f"- **Events:** {len(events):,}",
        f"- **Exported:** {datetime.now().isoformat(timespec='seconds')}",
        "",
        "---",
        "",
    ]

    turn = 0
    for ev in events:
        etype = ev.get("type")
        data = ev.get("data", {}) or {}
        stamp = fmt_time(ev.get("timestamp"))

        if etype == "user.message":
            turn += 1
            out += [f"## 👤 User (turn {turn})  <sub>{stamp}</sub>", "",
                    data.get("content", ""), "", "---", ""]

        elif etype == "assistant.message":
            content = (data.get("content") or "").strip()
            if content:
                out += [f"### 🤖 Assistant  <sub>{stamp}</sub>", "", content, ""]
            for req in data.get("toolRequests") or []:
                name = req.get("name", "?")
                args = req.get("arguments", {}) or {}
                summary = req.get("intentionSummary") or args.get("description") or ""
                out += [f"<details><summary>🔧 <code>{name}</code>: {summary}</summary>", ""]
                body = args.get("command") or args.get("query") or args.get("file_text")
                if body:
                    out += ["```", clip(str(body), MAX_ARG_CHARS), "```", ""]
                else:
                    out += ["```json",
                            clip(json.dumps(args, indent=2), MAX_ARG_CHARS), "```", ""]
                out += ["</details>", ""]

        elif etype == "tool.execution_complete":
            name, summary = call_index.get(data.get("toolCallId", ""), ("tool", ""))
            result = unwrap_result(data.get("result"))
            if result:
                ok = "✅" if data.get("success", True) else "❌"
                label = f"{ok} Result: <code>{name}</code>"
                if summary:
                    label += f" ({summary})"
                out += [f"<details><summary>{label}</summary>", "",
                        "```", clip(result, MAX_RESULT_CHARS), "```", "",
                        "</details>", ""]

    return "\n".join(out) + "\n"


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else find_latest_session()
    if not path.exists():
        raise SystemExit(f"Session log not found: {path}")
    OUT_PATH.write_text(render(path), encoding="utf-8")
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUT_PATH} ({size_kb:,.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
