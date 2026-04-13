# CUA_Collector V1

![Demo](../Demo.gif)
![WindowsDemo](../WindowsDemo.gif)

Legacy manual collector. Output now defaults to `V1/data/`.

`CUA_Collector` records:

- `State_A`: screenshot before the action
- `Action`: click, drag, scroll, or hotkey
- `State_B`: screenshot after the action

## Quick Start

### Windows

`Windows` does **not** need `setup.sh` or `setup_extension.sh`.

```powershell
git clone https://github.com/Zdong104/CUA_Collector.git
cd CUA_Collector
cd V1
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python collector.py
```

Note: Windows currently supports **single monitor only**. Multi-monitor setups can cause incorrect coordinates.

### MacOS

`macOS` does **not** need `setup.sh` or `setup_extension.sh`.

```bash
git clone https://github.com/Zdong104/CUA_Collector.git
cd CUA_Collector
cd V1
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python collector.py
```

Note: macOS currently supports **single monitor only**. Multi-monitor setups can cause incorrect coordinates.

### Ubuntu Wayland

`setup.sh` and `setup_extension.sh` are only for `Ubuntu Wayland / GNOME`.

```bash
git clone https://github.com/Zdong104/CUA_Collector.git
cd CUA_Collector
cd V1
bash setup.sh

# log out and log back in once

source .venv/bin/activate
python collector.py
```

If you already installed Python and system dependencies manually, the minimum extra step on Wayland is:

```bash
bash setup_extension.sh
```

After running either setup script, you must **log out and log back in** before starting the collector.

## Hotkeys

| Hotkey | Action |
|--------|--------|
| `Ctrl+F8` | Start a new task |
| `Ctrl+F9` | Take pre-screenshot and wait for the next action |
| `Ctrl+F12` | End current task |
| `Esc` | Cancel current pending action |
| `Ctrl+C` | Quit |

## Output

```text
V1/data/
  index.json
  <task_id>/
    task.json
    screenshots/
      action_0001_before.png
      action_0001_after.png
```

## Citation

```bibtex
@misc{dong2026cuacollector,
  author = {Zihan Dong},
  title = {CUA_Collector},
  year = {2026},
  note = {Computer Use Agent behavior collection demo}
}
```

## License

Commercial Use: let's discuss by puma122707@gmail.com.

Non-Commercial Use: free.

Research Use: free.
