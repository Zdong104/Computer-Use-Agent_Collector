"""
Run an OpenAI-compatible vision model through the CUA Agent Collector.

Expected flow:
  1. Start the collector in another terminal:
       cd ..
       ./run.sh --screen-index 3
  2. Start your model server, for example:
       BASE_URL=http://localhost:8000/v1
  3. Run this script with a task goal:
       python model_runner.py --task "Search Google for stock price today"

The collector owns capture, input injection, and task.json recording. This
script only chooses the next action from screenshots and calls the collector API.
"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
import base64
import io
from pathlib import Path
from typing import Any, Dict, Optional

from agent_client import AgentClient
from PIL import Image


ACTION_SCHEMA = """
Return exactly one JSON object and no markdown.

Allowed actions (screenshot-local coordinates; (0,0) = top-left of the image):
{"type":"move","x":300,"y":14}                              # moveTo(x,y)
{"type":"click","x":300,"y":14,"button":"left"}             # click(x,y)
{"type":"double_click","x":300,"y":14,"button":"left"}      # doubleClick(x,y)
{"type":"right_click","x":300,"y":14}                       # rightClick(x,y)
{"type":"drag","press_x":100,"press_y":100,"release_x":500,"release_y":500,"button":"left","duration":0.5}  # dragTo
{"type":"scroll","x":900,"y":700,"dy":-5,"dx":0}            # scroll(n)
{"type":"press_key","key":"enter"}                          # press('key')
{"type":"write","text":"stock price today"}                # write('text') / typewrite
{"type":"hotkey","keys":["ctrl","t"]}                       # hotkey('ctrl','t')
{"type":"key_down","key":"shift"}                           # keyDown('key')
{"type":"key_up","key":"shift"}                             # keyUp('key')
{"type":"mouse_down","x":300,"y":14,"button":"left"}        # mouseDown()
{"type":"mouse_up","x":300,"y":14,"button":"left"}          # mouseUp()
{"type":"wait","seconds":1.0}                               # WAIT (let the screen update)
{"type":"done","reason":"task complete"}                    # DONE (goal achieved)
{"type":"fail","reason":"task infeasible"}                  # FAIL (cannot proceed)

To type into a field, first click it to focus, THEN write. Prefer small,
deliberate actions. Do not repeat an action that produced no visible change.
Use "done" only when the goal has visibly been achieved; use "fail" if the task
is impossible from the current state.
""".strip()


def load_env(path: Path) -> None:
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


def _image_file_to_shot(path: Path) -> Dict[str, Any]:
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


def get_saved_screenshot(data_dir: str, task_id: str) -> Optional[Dict[str, Any]]:
    """Return the newest action_*_after.png the collector saved for this task.

    Steps 2+ reuse the collector's own recorded frames — perfectly aligned with
    what it injected, and no extra capture. Returns None if none exist yet.
    """
    if not task_id:
        return None
    shots_dir = Path(data_dir) / task_id / "screenshots"
    if not shots_dir.is_dir():
        return None
    afters = sorted(shots_dir.glob("action_*_after.png"))
    if not afters:
        return None
    # Highest sequence number = most recent post-action frame.
    return _image_file_to_shot(afters[-1])


def get_screenshot(collector_url: str, data_dir: str = "./data",
                   task_id: str = "", monitor_index: int = 0) -> Dict[str, Any]:
    # Prefer the collector's own saved post-action frame (steps 2+). Fall back to
    # a Wayland portal capture of the auto-detected screen for step 1 (no saved
    # frame yet), or if reading the saved frame fails.
    saved = None
    try:
        saved = get_saved_screenshot(data_dir, task_id)
    except Exception:
        saved = None
    if saved is not None:
        return saved
    return get_local_screenshot(monitor_index=monitor_index)


def get_local_screenshot(monitor_index: int = 0) -> Dict[str, Any]:
    # Wayland-compatible capture of the same monitor the collector controls.
    try:
        import wayland_screenshot
        return wayland_screenshot.capture(monitor_index=monitor_index or None)
    except Exception as e:
        raise RuntimeError(
            "Could not capture a screenshot via the Wayland portal. Ensure "
            "jeepney is installed and the collector reported a capture_screen."
        ) from e


def ask_model(base_url: str, model: str, api_key: str, task: str,
              screenshot: Dict[str, Any], step: int, history: list) -> Dict[str, Any]:
    image_url = f"data:{screenshot['mime_type']};base64,{screenshot['image_base64']}"
    user_text = (
        f"Task goal: {task}\n"
        f"Step: {step}\n"
        f"Screenshot size: {screenshot['width']}x{screenshot['height']}\n"
        f"Recent actions: {json.dumps(history[-8:], ensure_ascii=True)}\n\n"
        "Choose the next GUI action."
    )
    payload = {
        "model": model,
        "temperature": float(os.environ.get("TEMPERATURE", "0")),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a desktop GUI control agent. You inspect screenshots "
                    "and choose exactly one next action.\n\n" + ACTION_SCHEMA
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
    }
    res = http_json("POST", f"{base_url}/chat/completions", payload, api_key=api_key)
    content = res["choices"][0]["message"]["content"]
    return extract_json_object(content)


def run_collector_action(client: AgentClient, action: Dict[str, Any]) -> Dict[str, Any]:
    action_type = action.get("type") or action.get("action")
    if not action_type:
        raise ValueError(f"Model action is missing type: {action}")
    action_type = str(action_type).lower()

    if action_type == "wait":
        time.sleep(float(action.get("seconds", 1.0)))
        return {"status": "success", "waited": action.get("seconds", 1.0)}
    if action_type == "done":
        return {"status": "done", "reason": action.get("reason", "")}
    if action_type == "fail":
        return {"status": "fail", "reason": action.get("reason", "")}
    if action_type == "click":
        return client.click(int(action["x"]), int(action["y"]), action.get("button", "left"))
    if action_type == "double_click":
        return client.double_click(int(action["x"]), int(action["y"]), action.get("button", "left"))
    if action_type == "right_click":
        return client.send_action({"type": "right_click", "x": int(action["x"]), "y": int(action["y"])})
    if action_type == "move":
        return client.send_action({"type": "move", "x": int(action["x"]), "y": int(action["y"])})
    if action_type == "drag":
        return client.drag(
            int(action["press_x"]), int(action["press_y"]),
            int(action["release_x"]), int(action["release_y"]),
            action.get("button", "left"), float(action.get("duration", 0.5)),
        )
    if action_type == "write":
        return client.write(str(action["text"]))
    if action_type == "press_key":
        return client.press_key(str(action["key"]))
    if action_type == "hotkey":
        return client.hotkey(*[str(k) for k in action["keys"]])
    if action_type in ("key_down", "key_up"):
        return client.send_action({"type": action_type, "key": str(action["key"])})
    if action_type in ("mouse_down", "mouse_up"):
        payload = {"type": action_type, "button": action.get("button", "left")}
        if "x" in action:
            payload["x"] = int(action["x"])
        if "y" in action:
            payload["y"] = int(action["y"])
        return client.send_action(payload)
    if action_type == "scroll":
        return client.scroll(
            dx=int(action.get("dx", 0)),
            dy=int(action.get("dy", 0)),
            x=int(action["x"]) if "x" in action else None,
            y=int(action["y"]) if "y" in action else None,
        )
    raise ValueError(f"Unsupported model action type: {action_type}")


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    load_env(script_dir / ".env")

    parser = argparse.ArgumentParser(description="Run a model-controlled CUA recording task")
    parser.add_argument("--task", default=os.environ.get("TASK_DESCRIPTION", "Agent automated task"))
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000/v1"))
    parser.add_argument("--model", default=os.environ.get("MODEL", "default"))
    parser.add_argument("--api-key", default=os.environ.get("API_KEY", os.environ.get("OPENAI_API_KEY", "")))
    parser.add_argument("--collector-url", default=os.environ.get("COLLECTOR_URL", "http://127.0.0.1:8321"))
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "./data"),
                        help="Collector data dir holding <task_id>/screenshots (for saved-shot reuse)")
    parser.add_argument("--max-steps", type=int, default=int(os.environ.get("MAX_STEPS", "20")))
    parser.add_argument("--step-delay", type=float, default=float(os.environ.get("STEP_DELAY", "0.5")))
    parser.add_argument("--no-end-task", action="store_true")
    args = parser.parse_args()

    base_url = normalize_base_url(args.base_url)
    collector_url = args.collector_url.rstrip("/")
    client = AgentClient(collector_url)

    status = client.get_status()

    # Auto-detect the screen the collector selected at startup (from run.sh).
    # No manual re-selection: whatever the user picked is what we capture.
    capture_screen = status.get("capture_screen") or {}
    monitor_index = int(capture_screen.get("index", 0) or 0)
    if capture_screen:
        print(f"Collector capture screen: #{monitor_index} "
              f"{capture_screen.get('name', '')} "
              f"{capture_screen.get('width')}x{capture_screen.get('height')}")
    else:
        print("Collector did not report a capture_screen; portal capture will "
              "use the full desktop. Update the collector to expose it.")

    if not status.get("task_active"):
        start = client.start_task(args.task)
        task_id = start.get("task_id", "")
        print(f"Started collector task: {task_id}")
        time.sleep(1.0)
    else:
        task_id = status.get("task_id", "")
        print(f"Using active collector task: {task_id}")

    history = []
    done = False
    for step in range(1, args.max_steps + 1):
        screenshot = get_screenshot(collector_url, data_dir=args.data_dir,
                                    task_id=task_id, monitor_index=monitor_index)
        src = "saved" if screenshot.get("source") else "portal"
        action = ask_model(base_url, args.model, args.api_key, args.task, screenshot, step, history)
        print(f"Step {step} [{src} {screenshot['width']}x{screenshot['height']}]: "
              f"{json.dumps(action, ensure_ascii=True)}")
        result = run_collector_action(client, action)
        history.append({"step": step, "action": action, "result": result})

        if result.get("status") == "done":
            done = True
            print(f"Model reported done: {result.get('reason', '')}")
            break
        if result.get("status") == "fail":
            done = True
            print(f"Model reported fail: {result.get('reason', '')}")
            break
        time.sleep(args.step_delay)

    if not args.no_end_task:
        try:
            end = client.end_task()
            print(f"Ended collector task: {end}")
        except Exception as e:
            print(f"Could not end collector task cleanly: {e}")

    if not done:
        print(f"Stopped after max_steps={args.max_steps}.")


if __name__ == "__main__":
    main()
