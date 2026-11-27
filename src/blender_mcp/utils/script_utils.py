"""Utilities for safe Blender script generation."""

import json
import re


def py_str(value: str) -> str:
    """Return a JSON-quoted Python string literal safe to embed in generated scripts.

    Use this instead of bare f-string interpolation when embedding user-supplied
    strings into generated Blender Python scripts.  The result includes the outer
    quotes, so it can be dropped directly into an expression:

        script = f"obj = bpy.data.objects[{py_str(object_name)}]"
    """
    return json.dumps(str(value))


def sanitize_script_id(raw: str) -> str:
    """Return a filesystem-safe script ID limited to [A-Za-z0-9_-]{1,64}."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    return safe[:64] if safe else "script"
