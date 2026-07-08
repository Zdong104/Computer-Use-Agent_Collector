"""
Local web control panel for the CUA agent extension (pipeline hub).

Serves a single-page control panel on http://127.0.0.1:8300 that lets a user:
  - start/stop a human recording (proxied to the collector on :8321)
  - label a recorded demo into a reference (data_labeled/<TITLE>)
  - pick a reference, set trajectory-memory steps (n) and a max work-step budget,
    type a task, and launch a model run
  - watch the live trajectory (description + action per step) and a green DONE
    effect when the run finishes

Uses only the Python standard library. Long-running work (labeling, runs) happens
off the request thread; the UI polls JSON endpoints for progress.

    python control_panel.py            # then open http://127.0.0.1:8300
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import urllib.request
import urllib.error

import agent_common as ac

SCRIPT_DIR = Path(__file__).resolve().parent
ac.load_env(SCRIPT_DIR / ".env")

PANEL_HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8300"))
COLLECTOR_URL = os.environ.get("COLLECTOR_URL", "http://127.0.0.1:8321").rstrip("/")

# In-memory job registries (process-lifetime only).
LABEL_JOBS = {}          # job_id -> {state, title, done, total, msg, error}
RUNS = {}                # run_title -> {proc, log}


# ---------------------------------------------------------------------------
# Collector proxy helpers
# ---------------------------------------------------------------------------

def _collector(method: str, path: str, payload=None, timeout=15.0):
    url = f"{COLLECTOR_URL}{path}"
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode())


# ---------------------------------------------------------------------------
# Data listing
# ---------------------------------------------------------------------------

def list_demos():
    idx = ac.load_index(ac.data_dir() / "index.json")
    demos = [d for d in idx if d.get("num_actions", 0) > 0]
    demos.sort(key=lambda d: d.get("start_time", ""), reverse=True)
    return demos


def list_labeled():
    entries = ac.load_index(ac.labeled_index_path())
    entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return entries


def read_trajectory(title: str):
    p = ac.labeled_dir() / title / "task.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Jobs: labeling (thread) and runs (subprocess)
# ---------------------------------------------------------------------------

def start_label_job(task_id: str, title: str, force: bool = False):
    import annotate_reference
    # One active label job per recording; return the existing one if present.
    for jid, j in LABEL_JOBS.items():
        if j.get("task_id") == task_id and j.get("state") == "running":
            return jid

    job_id = uuid.uuid4().hex[:12]
    cancel = threading.Event()
    LABEL_JOBS[job_id] = {"state": "running", "task_id": task_id, "title": None,
                          "done": 0, "total": 0, "msg": "", "error": None, "_cancel": cancel}

    base_url = ac.normalize_base_url(os.environ.get("BASE_URL", "http://localhost:8000/v1"))
    model = os.environ.get("MODEL", "default")
    api_key = os.environ.get("API_KEY", os.environ.get("OPENAI_API_KEY", ""))

    def _cb(done, total, msg):
        LABEL_JOBS[job_id].update(done=done, total=total, msg=msg)

    def _work():
        try:
            entry = annotate_reference.label_task(
                task_id, base_url, model, api_key, title=title or None, force=force,
                progress_cb=_cb, should_cancel=cancel.is_set)
            LABEL_JOBS[job_id].update(state="done", title=entry["title"])
        except annotate_reference.LabelCancelled:
            LABEL_JOBS[job_id].update(state="cancelled")
        except Exception as e:
            LABEL_JOBS[job_id].update(state="error", error=str(e))

    threading.Thread(target=_work, daemon=True).start()
    return job_id


def cancel_label_job(job_id: str) -> bool:
    j = LABEL_JOBS.get(job_id)
    if j and j.get("state") == "running" and j.get("_cancel"):
        j["_cancel"].set()
        return True
    return False


def job_public(j):
    """Strip non-serializable internals from a job dict."""
    return {k: v for k, v in j.items() if not k.startswith("_")}


def start_run(task: str, reference_title: str, example_task_id: str,
              lookback: int, max_steps: int):
    # Pre-assign a unique BOT_ title so the UI can poll the trajectory file.
    existing = {p.name for p in ac.labeled_dir().iterdir()} if ac.labeled_dir().exists() else set()
    existing |= set(RUNS.keys())
    run_title = ac.unique_title(ac.make_title(task, prefix="BOT"), existing)

    cmd = [sys.executable, str(SCRIPT_DIR / "model_runner.py"),
           "--task", task, "--lookback", str(lookback), "--max-steps", str(max_steps),
           "--run-title", run_title]
    if reference_title:
        cmd += ["--reference", reference_title]
    elif example_task_id:
        cmd += ["--example", example_task_id]

    logs_dir = ac.labeled_dir() / ".run_logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / (run_title + ".log")
    log_f = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, cwd=str(SCRIPT_DIR))
    RUNS[run_title] = {"proc": proc, "log": str(log_path)}
    return run_title


def _log_tail(info, n=12):
    try:
        lines = Path(info["log"]).read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def run_state(title: str):
    traj = read_trajectory(title)
    info = RUNS.get(title)
    proc_alive = bool(info and info["proc"].poll() is None)
    status = (traj or {}).get("status", "running")
    if not proc_alive and status == "running":
        # Subprocess exited without finalizing -> treat as failed.
        status = "failed"
    # Failure reason: prefer the trajectory's own error, else the run log tail.
    error = (traj or {}).get("error", "")
    if status == "failed" and not error and info:
        error = _log_tail(info)
    return {"title": title, "status": status, "proc_alive": proc_alive,
            "error": error, "trajectory": traj}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            html = (SCRIPT_DIR / "panel" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if path == "/api/collector/status":
            try:
                self._json(_collector("GET", "/api/status"))
            except Exception as e:
                self._json({"error": str(e), "online": False})
            return
        if path == "/api/demos":
            self._json({"demos": list_demos()})
            return
        if path == "/api/labeled":
            self._json({"labeled": list_labeled()})
            return
        if path == "/api/jobs":
            # Active/finished label jobs keyed by task_id (for demo button state).
            by_task = {}
            for jid, j in LABEL_JOBS.items():
                pub = job_public(j); pub["job_id"] = jid
                by_task[j.get("task_id")] = pub
            self._json({"jobs": by_task})
            return
        if path.startswith("/api/job/"):
            j = LABEL_JOBS.get(path.rsplit("/", 1)[-1])
            self._json(job_public(j) if j else {"error": "unknown job"})
            return
        if path.startswith("/api/run/"):
            self._json(run_state(path.rsplit("/", 1)[-1]))
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        try:
            if path == "/api/record/start":
                desc = body.get("description", "Human demonstration")
                self._json(_collector("POST", "/api/task/start", {"description": desc}))
                return
            if path == "/api/record/stop":
                self._json(_collector("POST", "/api/task/end", {}))
                return
            if path == "/api/label":
                job_id = start_label_job(body.get("task_id", ""), body.get("title", ""),
                                         bool(body.get("force", False)))
                self._json({"job_id": job_id})
                return
            if path == "/api/label/cancel":
                ok = cancel_label_job(body.get("job_id", ""))
                self._json({"cancelled": ok})
                return
            if path == "/api/run":
                title = start_run(
                    body.get("task", "Agent task"),
                    body.get("reference_title", ""),
                    body.get("example_task_id", ""),
                    int(body.get("lookback", 10)),
                    int(body.get("max_steps", 20)))
                self._json({"run_title": title})
                return
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        self._json({"error": "not found"}, 404)


def main():
    ac.labeled_dir()  # ensure it exists
    srv = ThreadingHTTPServer((PANEL_HOST, PANEL_PORT), Handler)
    print(f"CUA control panel: http://{PANEL_HOST}:{PANEL_PORT}")
    print(f"Proxying collector at {COLLECTOR_URL}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down control panel.")
        srv.shutdown()


if __name__ == "__main__":
    main()
