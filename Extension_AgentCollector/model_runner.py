"""
Run an OpenAI-compatible vision model through the CUA Agent Collector.

Expected flow:
  1. Start the collector in another terminal:
       cd ..
       ./run.sh
  2. Start your model server, for example:
       BASE_URL=http://localhost:8000/v1
  3. Run this script with a task goal (optionally with a reference demo + memory):
       python model_runner.py --task "Search Google for stock price today" \
           --reference GO_CHROME_SEARCH_STOCK --lookback 10

The collector owns capture, input injection, and its own task.json recording.
This script chooses the next pyautogui action from the current screenshot plus
memory of the last N steps and an optional labeled reference workflow, and it
saves the executed trajectory to ./data_labeled/BOT_<TITLE>/ so bot runs become
future references too.
"""

import argparse
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import agent_common as ac
from agent_client import AgentClient


ACTION_SCHEMA = """
Return exactly one JSON object and no markdown:
{"description":"<short: what you are about to do and why>","action":"<one pyautogui call>","expected":"<what should visibly happen after>"}

The "action" MUST be exactly one of these calls. Coordinates are screenshot-local, (0,0) = top-left of the image:
  pyautogui.moveTo(x=300, y=14)
  pyautogui.click(x=300, y=14, button='left')
  pyautogui.double_click(x=300, y=14)
  pyautogui.rightClick(x=300, y=14)
  pyautogui.moveTo(100, 100); pyautogui.dragTo(500, 500, button='left', duration=0.5)
  pyautogui.scroll(-5, x=900, y=700)         # negative scrolls down
  pyautogui.press('enter')                   # a single key
  pyautogui.write('stock price today')       # type text into the focused field
  pyautogui.hotkey('ctrl', 't')              # key combo
To finish or give up, set "action" to one of:
  DONE('reason')     # the goal is visibly achieved
  FAIL('reason')     # the task is impossible from here
  WAIT(1.0)          # let the screen update, then look again

To type into a field, first click it to focus, THEN write. Prefer small,
deliberate actions. Do not repeat an action that produced no visible change.

Completion discipline:
- Only use DONE after YOU have actually performed the actions needed this session
  and verified the goal on the current screen.
- The current screen may still show leftover state from a previous demonstration or
  run. A screen that merely looks finished is NOT proof that you did the task — do
  not report DONE on the first step before you have taken any action.
""".strip()


# ---------------------------------------------------------------------------
# Screenshots (current observation only)
# ---------------------------------------------------------------------------

def get_saved_screenshot(data_dir: Path, task_id: str) -> Optional[Dict[str, Any]]:
    """Return the newest action_*_after.png the collector saved for this task."""
    if not task_id:
        return None
    shots_dir = Path(data_dir) / task_id / "screenshots"
    if not shots_dir.is_dir():
        return None
    afters = sorted(shots_dir.glob("action_*_after.png"))
    if not afters:
        return None
    return ac.image_file_to_shot(afters[-1])


def get_screenshot(data_dir: Path, task_id: str = "", monitor_index: int = 0) -> Dict[str, Any]:
    # Prefer the collector's own saved post-action frame (steps 2+); fall back to
    # a fresh Wayland portal capture for step 1 or if reading the saved frame fails.
    try:
        saved = get_saved_screenshot(data_dir, task_id)
    except Exception:
        saved = None
    if saved is not None:
        return saved
    return get_local_screenshot(monitor_index=monitor_index)


def get_local_screenshot(monitor_index: int = 0) -> Dict[str, Any]:
    try:
        import wayland_screenshot
        return wayland_screenshot.capture(monitor_index=monitor_index or None)
    except Exception as e:
        raise RuntimeError(
            "Could not capture a screenshot via the Wayland portal. Ensure "
            "jeepney is installed and the collector reported a capture_screen."
        ) from e


# ---------------------------------------------------------------------------
# Prompt assembly: memory + reference workflow
# ---------------------------------------------------------------------------

def render_history(history: List[Dict[str, Any]], lookback: int) -> str:
    """Render the last `lookback` executed steps as text (no images)."""
    if not history:
        return "(no steps yet)"
    lines = []
    for h in history[-lookback:]:
        lines.append(f"#{h['step']} description: {h['description']} | "
                     f"action: {h['action']} | expected: {h['expected']}")
    return "\n".join(lines)


def load_reference(title: str) -> Optional[Dict[str, Any]]:
    if not title:
        return None
    p = ac.labeled_dir() / title / "task.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def render_reference(ref: Dict[str, Any], max_steps: int = 60) -> str:
    lines = [f"Reference goal: {ref.get('description','')}"]
    for i, a in enumerate(ref.get("actions", [])[:max_steps], 1):
        act = a.get("pyautogui")
        if not act:
            act = ac.to_pyautogui(ac.human_action_to_collector(
                a.get("action_type", ""), a.get("action_coords"), a.get("action_details")))
        lines.append(f"{i}. description: {a.get('description','')} | "
                     f"action: {act} | expected: {a.get('expected','')}")
    return "\n".join(lines)


def ask_model(base_url: str, model: str, api_key: str, task: str,
              screenshot: Dict[str, Any], step: int, history: List[Dict[str, Any]],
              lookback: int, reference_text: str, extra_note: str = "") -> Dict[str, Any]:
    image_url = f"data:{screenshot['mime_type']};base64,{screenshot['image_base64']}"

    parts = [f"Task goal: {task}", f"Step: {step}",
             f"Screenshot size: {screenshot['width']}x{screenshot['height']}"]
    if reference_text:
        parts.append(
            "Reference demonstration — an example of HOW to do a similar task. These "
            "are steps to reproduce, NOT actions you have already performed. Adapt them "
            "to the live screen; do not copy coordinates blindly:\n" + reference_text)
    parts.append("Steps YOU have actually executed so far this session "
                 "(oldest to newest, no images):\n" + render_history(history, lookback))
    if extra_note:
        parts.append(extra_note)
    parts.append("Look at the current screenshot and choose the next action.")
    user_text = "\n\n".join(parts)

    messages = [
        {"role": "system", "content": (
            "You are a desktop GUI control agent. You inspect the current screenshot "
            "and choose exactly one next pyautogui action.\n\n" + ACTION_SCHEMA)},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]},
    ]
    content = ac.chat_completion(base_url, model, api_key, messages,
                                 temperature=float(os.environ.get("TEMPERATURE", "0")))
    return ac.extract_json_object(content)


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------

def run_collector_action(client: AgentClient, action: Dict[str, Any]) -> Dict[str, Any]:
    t = (action.get("type") or "").lower()
    if t == "wait":
        time.sleep(float(action.get("seconds", 1.0)))
        return {"status": "success", "waited": action.get("seconds", 1.0)}
    if t == "done":
        return {"status": "done", "reason": action.get("reason", "")}
    if t == "fail":
        return {"status": "fail", "reason": action.get("reason", "")}
    if t == "click":
        return client.click(int(action["x"]), int(action["y"]), action.get("button", "left"))
    if t == "double_click":
        return client.double_click(int(action["x"]), int(action["y"]), action.get("button", "left"))
    if t == "right_click":
        return client.send_action({"type": "right_click", "x": int(action["x"]), "y": int(action["y"])})
    if t == "move":
        return client.send_action({"type": "move", "x": int(action["x"]), "y": int(action["y"])})
    if t == "drag":
        return client.drag(int(action["press_x"]), int(action["press_y"]),
                           int(action["release_x"]), int(action["release_y"]),
                           action.get("button", "left"), float(action.get("duration", 0.5)))
    if t == "write":
        return client.write(str(action["text"]))
    if t == "press_key":
        return client.press_key(str(action["key"]))
    if t == "hotkey":
        return client.hotkey(*[str(k) for k in action["keys"]])
    if t == "scroll":
        return client.scroll(dx=int(action.get("dx", 0)), dy=int(action.get("dy", 0)),
                             x=int(action["x"]) if action.get("x") is not None else None,
                             y=int(action["y"]) if action.get("y") is not None else None)
    raise ValueError(f"Unsupported action type: {t}")


def coerce_action(model_out: Dict[str, Any]) -> Dict[str, Any]:
    """Turn the model's `action` (a pyautogui string, or a dict) into a collector action."""
    a = model_out.get("action")
    if isinstance(a, dict) and a.get("type"):
        return a
    if isinstance(a, str) and a.strip():
        return ac.parse_pyautogui(a)
    # Legacy fallback: a bare {"type": ...} object at the top level.
    if model_out.get("type"):
        return model_out
    raise ValueError(f"Model output has no usable action: {model_out}")


# ---------------------------------------------------------------------------
# Bot trajectory persistence (data_labeled/BOT_<TITLE>/)
# ---------------------------------------------------------------------------

def save_bot_trajectory(bot_dir: Path, meta: Dict[str, Any],
                        steps: List[Dict[str, Any]], status: str) -> None:
    record = dict(meta)
    record["status"] = status
    record["num_steps"] = len(steps)
    record["actions"] = steps
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "task.json").write_text(json.dumps(record, indent=2))


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    ac.load_env(script_dir / ".env")

    parser = argparse.ArgumentParser(description="Run a model-controlled CUA recording task")
    parser.add_argument("--task", default=os.environ.get("TASK_DESCRIPTION", "Agent automated task"))
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000/v1"))
    parser.add_argument("--model", default=os.environ.get("MODEL", "default"))
    parser.add_argument("--api-key", default=os.environ.get("API_KEY", os.environ.get("OPENAI_API_KEY", "")))
    parser.add_argument("--collector-url", default=os.environ.get("COLLECTOR_URL", "http://127.0.0.1:8321"))
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", str(ac.data_dir())),
                        help="Collector data dir holding <task_id>/screenshots (for saved-shot reuse)")
    parser.add_argument("--reference", default="", help="data_labeled/<TITLE> to use as guidance")
    parser.add_argument("--example", default="", help="data/<task_id> to auto-label into a reference first")
    parser.add_argument("--lookback", type=int, default=int(os.environ.get("LOOKBACK", "10")),
                        help="How many previous steps (with reasoning) the model remembers")
    parser.add_argument("--run-title", default="", help="Pre-assigned BOT_<TITLE> for the saved trajectory")
    parser.add_argument("--max-steps", type=int, default=int(os.environ.get("MAX_STEPS", "20")),
                        help="Max agent work-step budget")
    parser.add_argument("--retries", type=int, default=int(os.environ.get("RETRIES", "2")),
                        help="Extra model-call retries per step on transient errors (timeouts, bad JSON)")
    parser.add_argument("--step-delay", type=float, default=float(os.environ.get("STEP_DELAY", "0.5")))
    parser.add_argument("--no-end-task", action="store_true")
    args = parser.parse_args()

    base_url = ac.normalize_base_url(args.base_url)
    data_dir = Path(args.data_dir)
    client = AgentClient(args.collector_url.rstrip("/"))

    # Optionally label an example demo first, then use it as the reference.
    reference_title = args.reference
    if args.example and not reference_title:
        import annotate_reference
        print(f"Labeling example demo {args.example} ...")
        entry = annotate_reference.label_task(args.example, base_url, args.model, args.api_key)
        reference_title = entry["title"]
        print(f"Using reference: {reference_title}")

    reference = load_reference(reference_title) if reference_title else None
    reference_text = render_reference(reference) if reference else ""
    if reference_title and reference is None:
        print(f"[WARN] reference '{reference_title}' not found under data_labeled/; ignoring.")

    status = client.get_status()

    capture_screen = status.get("capture_screen") or {}
    monitor_index = int(capture_screen.get("index", 0) or 0)
    if capture_screen:
        print(f"Collector capture screen: #{monitor_index} {capture_screen.get('name','')} "
              f"{capture_screen.get('width')}x{capture_screen.get('height')}")

    if not status.get("task_active"):
        start = client.start_task(args.task)
        task_id = start.get("task_id", "")
        print(f"Started collector task: {task_id}")
        time.sleep(1.0)
    else:
        task_id = status.get("task_id", "")
        print(f"Using active collector task: {task_id}")

    # Pre-assign the bot trajectory directory (panel may pass --run-title).
    base_title = args.run_title or ac.make_title(args.task, prefix="BOT")
    existing = {p.name for p in ac.labeled_dir().iterdir()} if ac.labeled_dir().exists() else set()
    run_title = ac.unique_title(base_title, existing)
    bot_dir = ac.labeled_dir() / run_title
    meta = {
        "task_title": run_title,
        "kind": "bot",
        "description": args.task,
        "goal": args.task,
        "source_task_id": task_id,
        "reference": reference_title,
        "lookback": args.lookback,
        "model": args.model,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_bot_trajectory(bot_dir, meta, [], "running")
    print(f"Recording bot trajectory to data_labeled/{run_title}")

    history: List[Dict[str, Any]] = []
    steps: List[Dict[str, Any]] = []
    executed = 0            # real (non-meta) actions performed this session
    premature_guard_used = False
    done = False
    final_status = "running"
    fail_error = ""         # human-readable reason when a run fails

    PREMATURE_NOTE = (
        "IMPORTANT: You have not performed ANY action yet this session, so the task "
        "cannot already be complete. The current screen may show leftover state from a "
        "previous demonstration or run — ignore that as evidence of completion. Begin "
        "executing the task now with a concrete action.")

    for step in range(1, args.max_steps + 1):
        screenshot = get_screenshot(data_dir, task_id=task_id, monitor_index=monitor_index)
        src = "saved" if screenshot.get("source") else "portal"

        # Ask the model, retrying transient failures (timeouts, bad JSON) so one
        # slow response doesn't kill the whole run.
        model_out = action = None
        err = None
        for attempt in range(1, args.retries + 2):
            try:
                model_out = ask_model(base_url, args.model, args.api_key, args.task,
                                      screenshot, step, history, args.lookback, reference_text)
                action = coerce_action(model_out)
                # Guard: reject a premature DONE/FAIL before any real action was taken.
                if action.get("type") in ("done", "fail") and executed == 0 and not premature_guard_used:
                    premature_guard_used = True
                    print(f"Step {step}: premature '{action.get('type')}' with no actions taken — re-asking.")
                    model_out = ask_model(base_url, args.model, args.api_key, args.task,
                                          screenshot, step, history, args.lookback,
                                          reference_text, extra_note=PREMATURE_NOTE)
                    action = coerce_action(model_out)
                err = None
                break
            except Exception as e:
                err = e
                print(f"Step {step}: attempt {attempt}/{args.retries + 1} model/parse error: {e}")
                if attempt <= args.retries:
                    time.sleep(min(5.0, 2.0 * attempt))
        if err is not None:
            fail_error = f"step {step}: model/parse error after {args.retries + 1} attempts: {err}"
            print(fail_error)
            final_status = "failed"
            break

        description = str(model_out.get("description", "")).strip()
        expected = str(model_out.get("expected", "")).strip()
        action_str = model_out.get("action") if isinstance(model_out.get("action"), str) \
            else ac.to_pyautogui(action)
        print(f"Step {step} [{src} {screenshot['width']}x{screenshot['height']}] "
              f"{description}\n    -> {action_str}")

        try:
            result = run_collector_action(client, action)
        except Exception as e:
            result = {"status": "error", "error": str(e)}
            print(f"    action error: {e}")

        if action.get("type") not in ("done", "fail", "wait") and result.get("status") not in ("error",):
            executed += 1

        step_rec = {
            "sequence_number": step, "step": step,
            "description": description, "action": action_str,
            "expected": expected, "action_type": action.get("type"),
            "result": result,
        }
        history.append(step_rec)
        steps.append(step_rec)
        save_bot_trajectory(bot_dir, meta, steps, "running")

        st = result.get("status")
        if st == "done":
            done = True
            final_status = "done"
            print(f"Model reported done: {result.get('reason','')}")
            break
        if st == "fail":
            done = True
            final_status = "failed"
            fail_error = f"model reported FAIL: {result.get('reason','')}"
            print(fail_error)
            break
        time.sleep(args.step_delay)

    if final_status == "running":
        final_status = "done" if done else "stopped"
    meta["error"] = fail_error   # empty unless the run failed

    # Copy the collector's captured frames so the bot trajectory is self-contained.
    src_shots = data_dir / task_id / "screenshots"
    if src_shots.is_dir():
        dst_shots = bot_dir / "screenshots"
        if dst_shots.exists():
            shutil.rmtree(dst_shots)
        try:
            shutil.copytree(src_shots, dst_shots)
        except Exception as e:
            print(f"[WARN] could not copy screenshots: {e}")

    save_bot_trajectory(bot_dir, meta, steps, final_status)
    ac.upsert_index(ac.labeled_index_path(), {
        "title": run_title, "kind": "bot", "goal": args.task,
        "source_task_id": task_id, "num_steps": len(steps),
        "created_at": meta["created_at"], "model": args.model,
        "status": final_status, "error": fail_error,
    }, key="title")

    if not args.no_end_task:
        try:
            client.end_task()
            print("Ended collector task.")
        except Exception as e:
            print(f"Could not end collector task cleanly: {e}")

    print(f"Run {run_title} finished: status={final_status}, steps={len(steps)}")


if __name__ == "__main__":
    main()
