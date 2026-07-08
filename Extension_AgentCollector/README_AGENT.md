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

## Run A Model Task

Edit `.env` if needed:

```bash
BASE_URL=http://localhost:8000/v1
MODEL=default
COLLECTOR_URL=http://127.0.0.1:8321
```

In another terminal, then run:

```bash
cd Extension_AgentCollector
python model_runner.py --task "Search Google for stock price today"
```

The runner loops over:

1. `GET /api/screenshot` from the collector.
2. `POST $BASE_URL/chat/completions` with the screenshot and task.
3. One JSON action from the model.
4. `POST /api/action` to execute that action.

The collector remains responsible for recording actions, matching pre/post
screenshots, and writing `data/<task_id>/task.json`.
