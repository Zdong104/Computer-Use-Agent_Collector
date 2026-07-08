# Computer Use Agent Behavior Cloning: Collect and Replay

Open-source infrastructure for collecting, labeling, and replaying computer-use
agent trajectories across desktops and multimodal models.

CUA Collector is built for community-driven Computer Use Agent research. It
records human desktop demonstrations, turns them into labeled multimodal
trajectories, and replays them through any OpenAI-compatible vision model. The
project is designed to avoid platform lock-in, model lock-in, and closed data
pipelines.

![Demo](Demo.gif)

## Why This Project

### Open Community Collaboration

The project is intended to be a shared foundation for collecting and improving
computer-use behavior data. Researchers, builders, and agent developers can use
the same pipeline to contribute demonstrations, compare models, label tasks, and
turn successful agent runs into future references.

### Multimodal Model Support

CUA Collector talks to vision-capable models through an OpenAI-compatible API.
You can point it at local servers, hosted endpoints, or custom model gateways by
changing `BASE_URL`, `MODEL`, and `API_KEY`.

The agent loop sends the current screenshot plus text trajectory memory, receives
a structured next action, executes it, and records the result.

### Platform-Independent Design

The collector separates desktop capture/input control from model reasoning. That
means the same data and agent workflow can run across different desktop stacks
and model backends.

Current launchers include:

| Platform | Launcher | Backend |
|---|---|---|
| Ubuntu Wayland / GNOME | `./run.sh` | Native PipeWire + libevdev capture engine |
| Linux X11 | `./run_x11.sh` | Python `mss` + `pynput` backend |
| Windows | `.\run_win.ps1` | Native Win32 backend when built, fallback backend otherwise |
| macOS | `./run_mac.sh` | Python cross-platform backend |

## Workflow

CUA Collector is organized around three steps:

1. **Record** a human demonstration.
2. **Label** the demonstration into a reusable trajectory reference.
3. **Replay / run** a task with a multimodal model using optional reference
   guidance and trajectory memory.

### 1. Record

Start the collector, open the web panel, describe the task, then perform the task
manually. The collector captures actions automatically while the task is active.

<!-- Insert image here: recording a human demonstration in the web panel -->

### 2. Label

Select a recorded demo and label it. The labeling pass asks the configured model
to describe each action and its expected outcome, producing a reusable reference
under `data_labeled/<TITLE>/`.

<!-- Insert image here: labeling a recorded demonstration -->

### 3. Replay / Run

Choose a task, optionally select a labeled reference, set trajectory memory and
step budget, then start an agent run. The model sees the current screenshot,
recent trajectory memory, and optional reference steps, then returns one
structured action at a time.

Each run is saved as `data_labeled/BOT_<TITLE>/`, so successful agent behavior
can become training or guidance data for future runs.

<!-- Insert image here: replaying/running a task with a reference trajectory -->

## Web Control Panel

The recommended interface is the local web control panel:

- **Left side:** agent control panel for recording, labeling, references, memory,
  step budget, and live trajectory.
- **Right side:** the real desktop environment where the task is executed.

This layout makes it easy to supervise an agent while seeing both its decisions
and the actual UI operations.

<!-- Insert image here: left web panel, right desktop execution -->

## Quick Start

### Ubuntu Wayland / GNOME

```bash
git clone https://github.com/Zdong104/CUA_Collector.git
cd CUA_Collector
bash setup.sh
.venv/bin/pip install -r Extension_AgentCollector/requirements.txt

# Log out and log back in once so input permissions and the GNOME extension load.

./run.sh
```

In another terminal, start the web control panel:

```bash
cd Extension_AgentCollector
../.venv/bin/python control_panel.py
```

Then open:

```text
http://127.0.0.1:8300
```

The collector exposes its local control API at `http://127.0.0.1:8321`.

### Configure A Model

Edit `Extension_AgentCollector/.env`:

```bash
BASE_URL=http://localhost:8000/v1
MODEL=default
API_KEY=
COLLECTOR_URL=http://127.0.0.1:8321
```

Any OpenAI-compatible vision endpoint can be used.

### Linux X11

```bash
git clone https://github.com/Zdong104/CUA_Collector.git
cd CUA_Collector
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

./run_x11.sh
```

### Windows

```powershell
git clone https://github.com/Zdong104/CUA_Collector.git
cd CUA_Collector
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

cmake -S . -B build -DPython3_EXECUTABLE="$PWD\.venv\Scripts\python.exe"
cmake --build build --config Release
.\run_win.ps1
```

If PowerShell blocks script execution:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_win.ps1
```

### macOS

```bash
git clone https://github.com/Zdong104/CUA_Collector.git
cd CUA_Collector
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

./run_mac.sh
```

Grant Screen Recording and Accessibility permissions to the terminal app or
Python executable, then restart the launcher.

## CLI Usage

The web panel is recommended, but each stage also has a CLI equivalent.

Label a recorded human demo:

```bash
cd Extension_AgentCollector
python annotate_reference.py --task-id 20260706_204844_5ceecc8c
```

Run an agent task with a labeled reference:

```bash
python model_runner.py \
  --task "Search Google for the stock price today" \
  --reference GO_CHROME_SEARCH_STOCK \
  --lookback 10 \
  --max-steps 20
```

Auto-label an example demo first, then run against it:

```bash
python model_runner.py \
  --task "Search Google for the stock price today" \
  --example 20260706_204844_5ceecc8c \
  --lookback 10
```

## Data Layout

Raw recordings are saved under `data/`:

```text
data/
  index.json
  <task_id>/
    task.json
    screenshots/
      action_0001_before.png
      action_0001_after.png
```

Labeled human references and agent runs are saved under `data_labeled/`:

```text
data_labeled/
  index.json
  <TITLE>/
    task.json
    screenshots/
  BOT_<TITLE>/
    task.json
    screenshots/
```

Each labeled step includes:

- `description`: what the human or model is doing.
- `action`: the executable GUI action, usually represented as a `pyautogui`
  call.
- `expected`: what should visibly happen after the action.

## Hotkeys

| Hotkey | Action |
|---|---|
| `Ctrl+F8` | Start a new task |
| `Ctrl+F12` | End the current task |
| `Ctrl+C` | Quit the collector |

The current collector captures actions automatically while a task is active.

## Architecture

```text
Human / Agent Action
        |
        v
Desktop Capture + Input Backend
        |
        v
Raw trajectory in data/<task_id>/
        |
        v
Model labeling
        |
        v
Reusable reference in data_labeled/<TITLE>/
        |
        v
Model replay / agent run
        |
        v
New trajectory in data_labeled/BOT_<TITLE>/
```

The native Linux Wayland collector uses a C++ capture engine:

- PipeWire screenshot stream
- in-memory ring buffer
- libevdev input monitoring
- pre-action and post-action screenshot correlation
- Python task management and local HTTP control API

The agent extension handles:

- web control panel
- model configuration
- reference labeling
- task execution
- live trajectory display
- bot trajectory persistence

## Repository Layout

```text
.
├── collector.py                    # automated collector and local API
├── agent_client.py                 # client for collector action API
├── cross_platform_capture.py       # fallback capture/input backend
├── run.sh                          # Ubuntu Wayland/GNOME launcher
├── run_x11.sh                      # Linux X11 launcher
├── run_win.ps1                     # Windows launcher
├── run_mac.sh                      # macOS launcher
├── setup.sh                        # Ubuntu Wayland/GNOME setup
├── setup_extension.sh              # GNOME cursor tracker extension setup
├── CMakeLists.txt                  # native capture module build
├── include/ src/ tests/            # C++ capture engine
├── Extension_AgentCollector/
│   ├── control_panel.py            # local web panel
│   ├── model_runner.py             # model-controlled replay/run loop
│   ├── annotate_reference.py       # trajectory labeling
│   ├── agent_common.py             # shared model/data helpers
│   └── panel/index.html            # web panel UI
├── data/                           # raw recordings
└── data_labeled/                   # labeled references and bot runs
```

## Contributing

Contributions are welcome. Useful areas include:

- new platform backends
- model adapter examples
- better labeling prompts and schemas
- dataset tools and validators
- replay evaluation metrics
- documentation and demo trajectories

The goal is to make computer-use data collection and replay reproducible,
inspectable, and open.

## Citation

```bibtex
@misc{dong2026cuacollector,
  author = {Zihan Dong},
  title = {ComputerUseAgent\_Collector},
  year = {2026},
  url = {https://github.com/Zdong104/Computer-Use-Agent_Collector},
  note = {Computer Use Agent behavior cloning: collect and replay}
}
```

## License

Research and non-commercial use are free.

For commercial use, contact: puma122707@gmail.com.
