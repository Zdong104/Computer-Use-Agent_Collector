# CUA_Collector

version 2.2 

![Demo](Demo.gif)
![WindowsDemo](WindowsDemo.gif)

Current root is the automated collector formerly developed under `V2/`.

Legacy manual collector is archived under [`V1/`](./V1).

## Root Layout

```text
.
├── collector.py          # current automated collector entrypoint
├── run.sh                # launch helper (handles libstdc++ preload)
├── CMakeLists.txt        # native module build
├── include/ src/ tests/  # C++ capture engine
├── data/                 # current collector output
└── V1/                   # archived manual collector
```

## Quick Start

### Ubuntu Wayland / GNOME

```bash
git clone https://github.com/Zdong104/CUA_Collector.git
cd CUA_Collector
bash setup.sh

# log out and log back in once

./run.sh
```

If you already installed dependencies manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cmake -S . -B build -DPython3_EXECUTABLE=$PWD/.venv/bin/python3
cmake --build build -j$(nproc)
bash setup_extension.sh

# log out and log back in once

./run.sh
```

## Hotkeys

| Hotkey | Action |
|--------|--------|
| `Ctrl+F8` | Start a new task |
| `Ctrl+F12` | End current task |
| `Ctrl+C` | Quit |

The current collector captures actions automatically while a task is active.

## Output

```text
data/
  index.json
  <task_id>/
    task.json
    screenshots/
      action_0001_before.png
      action_0001_after.png
```

## Legacy V1

The original manual collector, setup scripts, and its existing datasets now live in [`V1/`](./V1).
Its output defaults to `V1/data/`.
