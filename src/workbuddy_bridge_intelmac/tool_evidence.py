"""Extract bounded evidence from known tools without retaining their output."""
import json
from pathlib import Path
import re


_MAX_TEXT = 1_048_576
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,199}\Z")
_FOUND = re.compile(r"Found (\d{1,7}) (?:matching )?tool\(s\)\.")
_CANDIDATES = re.compile(
    r"Found (\d{1,7}) tool\(s\) with full details and (\d{1,7}) additional candidate\(s\)\."
)


def _text_block(block: object) -> str | None:
    """Only native text blocks and the ACP content/text wrapper are accepted."""
    if not isinstance(block, dict):
        return None
    if block.get("type") == "content":
        inner = block.get("content")
        if not isinstance(inner, dict) or inner.get("type") != "text":
            return None
        block = inner
    if block.get("type") != "text":
        return None
    text = block.get("text")
    return text if isinstance(text, str) and len(text) <= _MAX_TEXT else None


def _text_blocks(blocks: list) -> str | None:
    if len(blocks) > 100:
        return None
    parts = []
    size = 0
    for block in blocks:
        text = _text_block(block)
        if text is None:
            continue
        size += len(text) + 1
        if size > _MAX_TEXT:
            return None
        parts.append(text)
    return "\n".join(parts) if parts else None


def _output_text(raw_output: object) -> str | None:
    """Accept native output wrappers, without traversing arbitrary fields."""
    if isinstance(raw_output, str):
        if len(raw_output) > _MAX_TEXT:
            return None
        try:
            parsed = json.loads(raw_output)
        except (ValueError, RecursionError):
            return raw_output
        raw_output = parsed
        if isinstance(raw_output, str):
            return raw_output
    if isinstance(raw_output, list):
        return _text_blocks(raw_output)
    if isinstance(raw_output, dict):
        text = _text_block(raw_output)
        if text is not None:
            return text
        if "type" in raw_output:
            return None
        for key in ("content", "output"):
            value = raw_output.get(key)
            if isinstance(value, str) and len(value) <= _MAX_TEXT:
                return value
            if isinstance(value, list):
                return _text_blocks(value)
            if isinstance(value, dict):
                text = _text_block(value)
                if text is not None:
                    return text
    return None


def _tool_search(text: str | None) -> dict:
    evidence = {"outcome": "unknown", "count": None, "tool_names": []}
    if not text:
        return evidence
    lines = text.splitlines()
    first = lines[0]
    if first.startswith("No matching tools found"):
        return {"outcome": "none", "count": 0, "tool_names": []}
    if first.startswith("Tool search failed:"):
        return {"outcome": "error", "count": None, "tool_names": []}
    matched = _CANDIDATES.match(first)
    if matched:
        count = int(matched[1]) + int(matched[2])
    else:
        matched = _FOUND.match(first)
        if not matched:
            return evidence
        count = int(matched[1])
    if count > 1_000_000:
        return evidence
    names: list[str] = []
    fenced = False
    candidates = False
    for line in lines[1:]:
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("Additional candidates (use ToolSearch"):
            candidates = True
            continue
        name = line[3:] if line.startswith("## ") else ""
        if candidates and line.startswith("- "):
            # The native renderer puts a colon plus a space before descriptions.
            name = line[2:].split(": ", 1)[0]
        elif candidates and line.startswith("Not found ("):
            candidates = False
        if _NAME.fullmatch(name) and name not in names:
            names.append(name)
            if len(names) == 50:
                break
    return {"outcome": "found", "count": count, "tool_names": names}


def extract_tool_evidence(tool_name: str, raw_output: object) -> dict:
    """Return only discovery names/counts or a verified user Skill directory."""
    if tool_name not in {"ToolSearch", "Skill"}:
        return {}
    text = _output_text(raw_output)
    if tool_name == "ToolSearch":
        return _tool_search(text)
    if not text:
        return {}
    prefix = "Base directory for this skill: "
    first = text.splitlines()[0]
    if not first.startswith(prefix):
        return {}
    path_text = first[len(prefix):]
    if not path_text or any(ord(char) < 32 for char in path_text):
        return {}
    path = Path(path_text)
    if not path.is_absolute() or "://" in path_text:
        return {}
    try:
        root = (Path.home() / ".workbuddy" / "skills").resolve()
        resolved = path.resolve()
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return {}
    if not relative.parts or len(resolved.name) > 200:
        return {}
    return {"skill_base_directory": str(resolved), "skill_name": resolved.name}
