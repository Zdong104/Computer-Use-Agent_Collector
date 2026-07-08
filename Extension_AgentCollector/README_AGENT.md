# Model-Controlled CUA Recording

This folder contains only the agent-side entrypoint and `.env` for
OpenAI-compatible model control. The original collector, native capture engine,
Wayland input backend, and `run.sh` stay in the repository root.

## Prerequisite: Grant `/dev/uinput` Access

The Wayland input backend injects clicks/keys through `/dev/uinput`, which is
root-only by default. Without this the collector returns
`HTTP 500: "/dev/uinput" cannot be opened for writing`. Run once (persists across
reboots), then make sure your user is in the `input` group (`sudo usermod -aG
input $USER` + re-login):

```bash
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' | sudo tee /etc/udev/rules.d/99-uinput.rules
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Start The Collector

```bash
cd ..
./run.sh
```

The root collector starts screen capture, creates the Wayland `/dev/uinput`
virtual devices, and exposes the local control API.

## Model Config

Edit `.env` if needed:

```bash
BASE_URL=http://localhost:8000/v1     # any OpenAI-compatible vision endpoint
MODEL=default
COLLECTOR_URL=http://127.0.0.1:8321
```

## Web Control Panel (recommended)

A local hub for the whole pipeline — record demos, label them, pick a reference,
set memory + budget, launch runs, and watch the live trajectory.

```bash
cd Extension_AgentCollector
python control_panel.py          # then open http://127.0.0.1:8300
```

The panel talks to the collector (`:8321`) and runs `model_runner.py` /
`annotate_reference.py` for you. Steps in the UI:

1. **Record a human demo** — type what it is, press *Start recording*, do the
   task by hand, press *Stop*. Saved to `../data/<task_id>/`.
2. **Label** a recorded demo — the model walks each action and writes a
   `description` + `expected`, producing a reference in `../data_labeled/<TITLE>/`.
3. **Configure & run** — enter a task, optionally pick a reference for guidance,
   set *trajectory memory (n)* and *max work steps*, press *Start task*.
4. Watch the **live trajectory** (one `description` + pyautogui `action` per step);
   a green **DONE** effect appears when the run finishes.

## Pipeline & Data Layout

```
data/                       raw human & agent recordings (unchanged)
  <task_id>/task.json, screenshots/
data_labeled/               labeled trajectory library
  index.json
  <TITLE>/                  human reference (e.g. GO_CHROME_SEARCH_STOCK)
    task.json               original schema + per-step description/expected + task_title
    screenshots/
  BOT_<TITLE>/              a bot run's own trajectory (becomes a future reference)
```

The model reads/returns actions as **pyautogui calls** (e.g.
`pyautogui.double_click(x=522, y=57)`), plus a `description` (its thinking) and an
`expected` outcome. Past steps are fed back as text (`description | action |
expected`, **no images**); only the current screenshot is sent each step, and the
model remembers the last `n` steps (default 10).

## CLI (headless equivalents)

```bash
# 1. Label a recorded demo into a reference
python annotate_reference.py --task-id 20260706_204844_5ceecc8c

# 2. Run a task, using a reference for guidance + 10-step memory
python model_runner.py --task "Search Google for the stock price today" \
    --reference GO_CHROME_SEARCH_STOCK --lookback 10 --max-steps 20

#    ...or auto-label an example demo first, then run against it
python model_runner.py --task "..." --example <task_id> --lookback 10
```

Each run saves its executed trajectory to `data_labeled/BOT_<TITLE>/task.json`
(with a `status` of `running` → `done`/`failed`/`stopped`), which the panel polls
for the live view.

The collector remains responsible for input injection, matching pre/post
screenshots, and writing `data/<task_id>/task.json`.
