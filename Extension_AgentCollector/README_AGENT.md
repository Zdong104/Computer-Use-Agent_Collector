# Model-Controlled CUA Recording

This folder contains only the agent-side entrypoint and `.env` for
OpenAI-compatible model control. The original collector, native capture engine,
Wayland input backend, and `run.sh` stay in the repository root.

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
