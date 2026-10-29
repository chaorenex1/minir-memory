"""minir_kb_write: file-level page writes for the value-marker writer."""

from __future__ import annotations

import json

from . import knowledge

TOOL_NAME = "minir_kb_write"
OPS = knowledge.OPS

SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Create or edit one Minir knowledge page at the file level. `create` writes the page "
        "header once; every other op addresses the body by line or by exact text. The body has "
        "no line count, line length or character cap: write the claims the page needs. Each call "
        "re-reads the file, verifies the change, and returns {status, file, path, org, category, "
        "topic, lines, bytes, sha256, verified, warnings, error}. A failed verification restores "
        "the previous bytes. Pass expect_sha from a search result to refuse a stale write."),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Relative page path, e.g. tools/KNOWLEDGE-duckpgq.md."},
            "op": {"type": "string", "enum": list(OPS)},
            "topic": {"type": "string", "description": "create only; its slug must match the file."},
            "summary": {"type": "string", "description": "create only; <=120 chars."},
            "epistemic": {"type": "string", "description": "create only; required on KNOWLEDGE."},
            "time": {"type": "string", "description": "create only; YYYY-MM-DD or empty."},
            "objects": {"type": "array", "items": {"type": "string"}},
            "relations": {"type": "array", "items": {"type": "object"}},
            "content": {"type": "string", "description": "Lines to write; empty only for delete."},
            "line": {"type": "integer", "description": "Body line, 1-based."},
            "end_line": {"type": "integer", "description": "Inclusive last line; defaults to line."},
            "old": {"type": "string", "description": "Exact text to match instead of a line."},
            "position": {"type": "string", "enum": ["before", "after"]},
            "expect_sha": {"type": "string", "description": "sha256 of the file you read."},
        },
        "required": ["path", "op"],
    },
}


def handle(args: dict, *, hermes_home: str, language: str = "en",
           source_session: str = "", budget: int = 0) -> str:
    """One file-level write; verifies on disk and never raises."""
    try:
        result = knowledge.edit_page(
            root=knowledge.knowledge_dir(hermes_home),
            path=str((args or {}).get("path") or ""),
            op=str((args or {}).get("op") or ""),
            header=args or {},
            content=str((args or {}).get("content") or ""),
            line=int((args or {}).get("line") or 0),
            end_line=int((args or {}).get("end_line") or 0),
            old=str((args or {}).get("old") or ""),
            position=str((args or {}).get("position") or "after"),
            expect_sha=str((args or {}).get("expect_sha") or ""),
            language=language,
            budget=int(budget),
            source_session=str(source_session or ""),
        )
    except Exception as exc:
        return json.dumps({"status": "failed", "path": "", "verified": False,
                           "error": "%s: %s" % (type(exc).__name__, exc)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)
