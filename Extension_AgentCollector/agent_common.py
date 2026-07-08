"""
Shared helpers for the CUA agent extension.

Groups together everything that model_runner.py, annotate_reference.py and
control_panel.py all need so there is a single source of truth for:

  - env / base-url loading and OpenAI-compatible HTTP calls
  - the clean on-disk database layout (data/ raw recordings, data_labeled/
    labeled trajectories), resolved at the repo root (never cwd-relative)
  - the pyautogui action vocabulary: render a collector/human action to a
    `pyautogui.*` string, and parse such a string back to a collector action
  - task-title generation / sanitising and the data_labeled index
"""

import ast
import base64
import io
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Database layout (absolute, anchored at the repo root)
# ---------------------------------------------------------------------------

def repo_root() -> Path:
    """Repo root = parent of the Extension_AgentCollector folder this file lives in."""
    return Path(__file__).resolve().parent.parent


def db_paths() -> Dict[str, Path]:
    root = repo_root()
    return {
        "root": root,
        "data": root / "data",                    # raw human & agent recordings
        "data_labeled": root / "data_labeled",    # labeled trajectory library
    }


def data_dir() -> Path:
    return db_paths()["data"]


def labeled_dir() -> Path:
    d = db_paths()["data_labeled"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def labeled_index_path() -> Path:
    return labeled_dir() / "index.json"


# ---------------------------------------------------------------------------
# Env / URL
# ---------------------------------------------------------------------------

def load_env(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (without overriding)."""
    path = Path(path)
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_base_url(url: str) -> str:
    # Be forgiving of the common typo "http:/localhost:8000/v1".
    if url.startswith("http:/") and not url.startswith("http://"):
        url = "http://" + url[len("http:/"):]
    if url.startswith("https:/") and not url.startswith("https://"):
        url = "https://" + url[len("https:/"):]
    return url.rstrip("/")


# ---------------------------------------------------------------------------
# HTTP JSON (OpenAI-compatible endpoints)
# ---------------------------------------------------------------------------

def http_json(method: str, url: str, payload: Optional[Dict[str, Any]] = None,
              api_key: str = "", timeout: float = 120.0) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed with HTTP {e.code}: {body}") from e


def extract_json_object(text: str) -> Dict[str, Any]:
    """Best-effort parse of a single JSON object from a model response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def chat_completion(base_url: str, model: str, api_key: str, messages: List[Dict[str, Any]],
                    temperature: float = 0.0, timeout: float = 120.0) -> str:
    """Call /chat/completions and return the assistant message content."""
    payload = {"model": model, "temperature": temperature, "messages": messages}
    res = http_json("POST", f"{base_url}/chat/completions", payload,
                    api_key=api_key, timeout=timeout)
    return res["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def image_file_to_data_url(path: Path) -> str:
    """Return a data: URL for a PNG/JPEG file on disk."""
    from PIL import Image
    with Image.open(path) as im:
        img = im.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def image_file_to_shot(path: Path) -> Dict[str, Any]:
    """Return a screenshot dict {width,height,mime_type,image_base64,source}."""
    from PIL import Image
    import time
    with Image.open(path) as im:
        img = im.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {
        "frame_id": 0,
        "timestamp": time.time(),
        "width": img.width,
        "height": img.height,
        "mime_type": "image/png",
        "image_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
        "source": str(path),
    }


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "a", "an", "the", "to", "of", "for", "and", "or", "in", "on", "at", "my",
    "me", "i", "with", "that", "this", "those", "these", "related", "please",
    "is", "are", "be", "into", "from", "by", "as", "your", "our", "it",
}


def sanitize_title(raw: str, max_words: int = 4, prefix: str = "") -> str:
    """Uppercase underscore-joined title of at most `max_words` significant words.

    A `prefix` (e.g. "BOT") is prepended and does not count toward max_words,
    so BOT_ + up to 4 words stays <= 5 tokens total.
    """
    words = re.findall(r"[A-Za-z0-9]+", raw or "")
    significant = [w for w in words if w.lower() not in _STOPWORDS]
    if not significant:
        significant = words or ["TASK"]
    picked = [w.upper() for w in significant[:max_words]]
    title = "_".join(picked)
    if prefix:
        title = f"{prefix.upper().strip('_')}_{title}"
    return title


def make_title(goal: str, prefix: str = "") -> str:
    return sanitize_title(goal, max_words=4, prefix=prefix)


def unique_title(base_title: str, existing: set) -> str:
    """Append _2, _3, ... if base_title collides with an existing directory name."""
    if base_title not in existing:
        return base_title
    n = 2
    while f"{base_title}_{n}" in existing:
        n += 1
    return f"{base_title}_{n}"


# ---------------------------------------------------------------------------
# pyautogui action vocabulary
# ---------------------------------------------------------------------------

# Recorded key names -> pyautogui-friendly key names.
_KEYMAP = {
    "ctrl_l": "ctrl", "ctrl_r": "ctrl", "control_l": "ctrl", "control_r": "ctrl",
    "shift_l": "shift", "shift_r": "shift",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "altright",
    "super": "win", "super_l": "win", "super_r": "win", "cmd": "win",
    "return": "enter", "escape": "esc",
    " ": "space",
}


def _norm_key(k: str) -> str:
    if k is None:
        return ""
    return _KEYMAP.get(k, _KEYMAP.get(str(k).lower(), str(k)))


def human_action_to_collector(action_type: str, coords: Optional[List[int]],
                              details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Convert a recorded human action (task.json) into a collector action dict.

    Collector action dicts are the same shape run_collector_action / the
    collector /api/action endpoint consume.
    """
    details = details or {}
    x, y = (coords or [0, 0])[:2]
    mouse = (details.get("mouse") or [{}])
    button = mouse[0].get("button", "left") if mouse else "left"

    if action_type == "click":
        return {"type": "click", "x": x, "y": y, "button": button}
    if action_type == "double_click":
        return {"type": "double_click", "x": x, "y": y, "button": button}
    if action_type == "right_click":
        return {"type": "right_click", "x": x, "y": y}
    if action_type == "drag":
        m = mouse[0] if mouse else {}
        px, py = (m.get("press_coords") or coords or [x, y])[:2]
        rx, ry = (m.get("release_coords") or coords or [x, y])[:2]
        return {"type": "drag", "press_x": px, "press_y": py,
                "release_x": rx, "release_y": ry, "button": button, "duration": 0.5}
    if action_type == "scroll":
        sc = details.get("scroll") or {}
        return {"type": "scroll", "dx": sc.get("dx_total", 0),
                "dy": sc.get("dy_total", 0), "x": x, "y": y}
    if action_type in ("hotkey", "press_key", "key"):
        keys = [_norm_key(k.get("key")) for k in (details.get("keys") or []) if k.get("key")]
        keys = [k for k in keys if k]
        if len(keys) == 1:
            return {"type": "press_key", "key": keys[0]}
        if keys:
            return {"type": "hotkey", "keys": keys}
        return {"type": "press_key", "key": ""}
    if action_type == "write":
        return {"type": "write", "text": details.get("text", "")}
    if action_type == "move":
        return {"type": "move", "x": x, "y": y}
    # Unknown -> a harmless move so rendering never crashes.
    return {"type": "move", "x": x, "y": y}


def to_pyautogui(action: Dict[str, Any]) -> str:
    """Render a collector action dict as a pyautogui call string (or DONE/FAIL/WAIT)."""
    t = (action.get("type") or "").lower()
    if t == "click":
        return f"pyautogui.click(x={action['x']}, y={action['y']}, button={action.get('button','left')!r})"
    if t == "double_click":
        return f"pyautogui.double_click(x={action['x']}, y={action['y']})"
    if t == "right_click":
        return f"pyautogui.rightClick(x={action['x']}, y={action['y']})"
    if t == "move":
        return f"pyautogui.moveTo(x={action['x']}, y={action['y']})"
    if t == "drag":
        return (f"pyautogui.moveTo({action['press_x']}, {action['press_y']}); "
                f"pyautogui.dragTo({action['release_x']}, {action['release_y']}, "
                f"button={action.get('button','left')!r}, duration={action.get('duration',0.5)})")
    if t == "write":
        return f"pyautogui.write({action.get('text','')!r})"
    if t == "press_key":
        return f"pyautogui.press({action.get('key','')!r})"
    if t == "hotkey":
        keys = ", ".join(repr(k) for k in action.get("keys", []))
        return f"pyautogui.hotkey({keys})"
    if t == "scroll":
        x, y = action.get("x"), action.get("y")
        if x is not None and y is not None:
            return f"pyautogui.scroll({action.get('dy',0)}, x={x}, y={y})"
        return f"pyautogui.scroll({action.get('dy',0)})"
    if t == "wait":
        return f"WAIT({action.get('seconds',1.0)})"
    if t == "done":
        return f"DONE({action.get('reason','')!r})"
    if t == "fail":
        return f"FAIL({action.get('reason','')!r})"
    return f"# unsupported: {json.dumps(action)}"


def _call_name_args(node: ast.Call):
    if isinstance(node.func, ast.Attribute):
        name = node.func.attr
    elif isinstance(node.func, ast.Name):
        name = node.func.id
    else:
        raise ValueError("Unrecognized call target")
    args = [ast.literal_eval(a) for a in node.args]
    kwargs = {k.arg: ast.literal_eval(k.value) for k in node.keywords}
    return name, args, kwargs


def _pos_or_kw(args, kwargs, idx, key, default=None):
    if key in kwargs:
        return kwargs[key]
    if idx < len(args):
        return args[idx]
    return default


def parse_pyautogui(s: str) -> Dict[str, Any]:
    """Parse a pyautogui string (or DONE/FAIL/WAIT) into a collector action dict.

    Handles the moveTo(...); dragTo(...) pair as a single drag. Uses ast so
    quoting/commas inside string arguments are handled correctly.
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("Empty action string")
    tree = ast.parse(s, mode="exec")
    calls = [n.value for n in tree.body
             if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    if not calls:
        raise ValueError(f"No callable action found in: {s!r}")

    parsed = [_call_name_args(c) for c in calls]

    # moveTo(...) ; dragTo(...) -> drag
    names = [p[0] for p in parsed]
    if "dragTo" in names:
        drag_i = names.index("dragTo")
        _, dargs, dkw = parsed[drag_i]
        rx = _pos_or_kw(dargs, dkw, 0, "x")
        ry = _pos_or_kw(dargs, dkw, 1, "y")
        button = dkw.get("button", "left")
        duration = dkw.get("duration", 0.5)
        px, py = rx, ry
        for i in range(drag_i):
            if parsed[i][0] in ("moveTo", "move"):
                _, margs, mkw = parsed[i]
                px = _pos_or_kw(margs, mkw, 0, "x")
                py = _pos_or_kw(margs, mkw, 1, "y")
        return {"type": "drag", "press_x": int(px), "press_y": int(py),
                "release_x": int(rx), "release_y": int(ry),
                "button": button, "duration": float(duration)}

    name, args, kwargs = parsed[0]

    if name in ("click",):
        return {"type": "click",
                "x": int(_pos_or_kw(args, kwargs, 0, "x", 0)),
                "y": int(_pos_or_kw(args, kwargs, 1, "y", 0)),
                "button": kwargs.get("button", "left")}
    if name in ("doubleClick", "double_click"):
        return {"type": "double_click",
                "x": int(_pos_or_kw(args, kwargs, 0, "x", 0)),
                "y": int(_pos_or_kw(args, kwargs, 1, "y", 0)),
                "button": kwargs.get("button", "left")}
    if name in ("rightClick", "right_click"):
        return {"type": "right_click",
                "x": int(_pos_or_kw(args, kwargs, 0, "x", 0)),
                "y": int(_pos_or_kw(args, kwargs, 1, "y", 0))}
    if name in ("moveTo", "move"):
        return {"type": "move",
                "x": int(_pos_or_kw(args, kwargs, 0, "x", 0)),
                "y": int(_pos_or_kw(args, kwargs, 1, "y", 0))}
    if name in ("write", "typewrite"):
        text = _pos_or_kw(args, kwargs, 0, "message", "")
        return {"type": "write", "text": str(text)}
    if name in ("press",):
        key = _pos_or_kw(args, kwargs, 0, "keys", "")
        if isinstance(key, (list, tuple)):
            return {"type": "hotkey", "keys": [str(k) for k in key]}
        return {"type": "press_key", "key": str(key)}
    if name in ("hotkey",):
        return {"type": "hotkey", "keys": [str(a) for a in args]}
    if name in ("scroll", "vscroll"):
        return {"type": "scroll", "dx": 0, "dy": int(_pos_or_kw(args, kwargs, 0, "clicks", 0)),
                "x": kwargs.get("x"), "y": kwargs.get("y")}
    if name in ("hscroll",):
        return {"type": "scroll", "dy": 0, "dx": int(_pos_or_kw(args, kwargs, 0, "clicks", 0)),
                "x": kwargs.get("x"), "y": kwargs.get("y")}
    if name == "WAIT":
        return {"type": "wait", "seconds": float(_pos_or_kw(args, kwargs, 0, "seconds", 1.0))}
    if name == "DONE":
        return {"type": "done", "reason": str(_pos_or_kw(args, kwargs, 0, "reason", ""))}
    if name == "FAIL":
        return {"type": "fail", "reason": str(_pos_or_kw(args, kwargs, 0, "reason", ""))}
    raise ValueError(f"Unsupported pyautogui action: {name}")


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------

def load_index(path: Path) -> List[Dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_index(path: Path, entries: List[Dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2))


def upsert_index(path: Path, entry: Dict[str, Any], key: str = "title") -> None:
    """Insert or replace an index entry keyed by `key` (e.g. the title)."""
    entries = load_index(path)
    entries = [e for e in entries if e.get(key) != entry.get(key)]
    entries.append(entry)
    write_index(path, entries)
