"""
Label a recorded human demonstration into a reusable reference trajectory.

Pipeline step 3: the model walks through a human recording in ./data and, for
each action, writes a short `description` (what the user is about to do) and an
`expected` outcome (what should visibly happen after). The result is copied to
./data_labeled/<TITLE>/ with the original recording intact plus those two fields
on every step and a <=5-word task title.

The labeler is read-only: it never re-executes the recorded actions.

CLI:
    python annotate_reference.py --task-id 20260707_224741_36b93451 [--title MY_TITLE] [--force]
"""

import argparse
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import agent_common as ac


LABEL_SYSTEM_PROMPT = (
    "You label a human GUI demonstration so an agent can later reuse it.\n"
    "You are shown the screenshot BEFORE one action and the exact pyautogui call "
    "the human performed toward the stated goal.\n"
    "Return exactly one JSON object, no markdown:\n"
    '{"description": "<short first-person thinking: what I am about to do and why>",'
    ' "expected": "<what should visibly happen on screen after this action>"}\n'
    "Keep each field to one concise sentence."
)


def _label_step(base_url: str, model: str, api_key: str, goal: str, step_no: int,
                total: int, action_str: str, pre_png: Optional[Path]) -> Dict[str, str]:
    user_content: List[Dict[str, Any]] = [{
        "type": "text",
        "text": (f"Goal: {goal}\nStep {step_no} of {total}\n"
                 f"Human action: {action_str}\n\n"
                 "Describe this step."),
    }]
    if pre_png and Path(pre_png).exists():
        user_content.append({
            "type": "image_url",
            "image_url": {"url": ac.image_file_to_data_url(pre_png)},
        })
    messages = [
        {"role": "system", "content": LABEL_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    try:
        content = ac.chat_completion(base_url, model, api_key, messages, temperature=0.0)
        obj = ac.extract_json_object(content)
        return {
            "description": str(obj.get("description", "")).strip(),
            "expected": str(obj.get("expected", "")).strip(),
        }
    except Exception as e:
        return {"description": f"Perform {action_str}", "expected": "", "error": str(e)}


class LabelCancelled(Exception):
    """Raised when a label job is cancelled before it finishes."""


def label_task(task_id: str, base_url: str, model: str, api_key: str,
               title: Optional[str] = None, force: bool = False,
               progress_cb: Optional[Callable[[int, int, str], None]] = None,
               should_cancel: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Label the recording data/<task_id> into data_labeled/<TITLE>. Returns its index entry."""
    src = ac.data_dir() / task_id
    task_json = src / "task.json"
    if not task_json.exists():
        raise FileNotFoundError(f"No recording at {task_json}")

    record = json.loads(task_json.read_text())
    goal = record.get("description", "") or task_id
    actions = record.get("actions", [])
    total = len(actions)

    labeled_root = ac.labeled_dir()
    index_path = ac.labeled_index_path()

    # Reuse a fresh label unless --force.
    if not force:
        for e in ac.load_index(index_path):
            if e.get("source_task_id") == task_id and e.get("kind") == "human":
                existing = labeled_root / e["title"]
                if (existing / "task.json").exists():
                    if progress_cb:
                        progress_cb(total, total, f"cached:{e['title']}")
                    return e

    # Pick a unique directory title.
    base_title = ac.sanitize_title(title or goal, max_words=4)
    existing_dirs = {p.name for p in labeled_root.iterdir()} if labeled_root.exists() else set()
    out_title = ac.unique_title(base_title, existing_dirs)
    out_dir = labeled_root / out_title

    # Label every step.
    for i, a in enumerate(actions, 1):
        if should_cancel and should_cancel():
            raise LabelCancelled(f"Labeling cancelled at step {i}/{total}")
        collector_action = ac.human_action_to_collector(
            a.get("action_type", ""), a.get("action_coords"), a.get("action_details"))
        action_str = ac.to_pyautogui(collector_action)
        pre = src / "screenshots" / a.get("pre_screenshot", "") if a.get("pre_screenshot") else None
        labels = _label_step(base_url, model, api_key, goal, i, total, action_str, pre)
        a["pyautogui"] = action_str
        a["description"] = labels["description"]
        a["expected"] = labels["expected"]
        if progress_cb:
            progress_cb(i, total, action_str)

    # Materialize the self-contained labeled copy.
    if out_dir.exists():
        shutil.rmtree(out_dir)
    src_shots = src / "screenshots"
    if src_shots.exists():
        shutil.copytree(src_shots, out_dir / "screenshots")
    else:
        out_dir.mkdir(parents=True, exist_ok=True)

    record["task_title"] = out_title
    record["source_task_id"] = task_id
    record["kind"] = "human"
    record["labeled_at"] = datetime.now(timezone.utc).isoformat()
    record["label_model"] = model
    (out_dir / "task.json").write_text(json.dumps(record, indent=2))

    entry = {
        "title": out_title,
        "kind": "human",
        "goal": goal,
        "source_task_id": task_id,
        "num_steps": total,
        "created_at": record["labeled_at"],
        "model": model,
    }
    ac.upsert_index(index_path, entry, key="title")
    return entry


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    ac.load_env(script_dir / ".env")

    parser = argparse.ArgumentParser(description="Label a human demonstration into a reference")
    parser.add_argument("--task-id", required=True, help="Recording id under ./data")
    parser.add_argument("--title", default=None, help="Override the generated title")
    parser.add_argument("--force", action="store_true", help="Re-label even if a cache exists")
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000/v1"))
    parser.add_argument("--model", default=os.environ.get("MODEL", "default"))
    parser.add_argument("--api-key", default=os.environ.get("API_KEY", os.environ.get("OPENAI_API_KEY", "")))
    args = parser.parse_args()

    base_url = ac.normalize_base_url(args.base_url)

    def _cb(i, total, msg):
        print(f"  [{i}/{total}] {msg}")

    print(f"Labeling recording {args.task_id} ...")
    t0 = time.time()
    entry = label_task(args.task_id, base_url, args.model, args.api_key,
                       title=args.title, force=args.force, progress_cb=_cb)
    print(f"Done in {time.time()-t0:.1f}s -> data_labeled/{entry['title']} "
          f"({entry['num_steps']} steps)")


if __name__ == "__main__":
    main()
