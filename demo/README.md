# Feishu Demo Playback

This folder contains demo-only tooling for showing a fast Feishu group conversation while feeding the backend memory pipeline directly.

## What It Does

`play_feishu_demo.py` reads `benchmark/full_demo_case.json` and:

- sends fixture messages quickly to a real Feishu group for front-end display;
- preserves the fixture time labels, such as `[04-29 10:00]`, while sending messages every few tenths of a second;
- uses role webhooks from `.env` when available, so PM/Dev/QA can appear as different custom bots;
- feeds the same fixture messages directly into the backend runtime entries:
  - `dispatch_message(...)`
  - `BatchProcessor.process_fetch_batch(...)`
- skips slow Graphiti card writes by default for demo speed.

This is a demo orchestrator, not a production replacement. Webhook bot messages are usually filtered by the real write-side Feishu fetch path, so the script feeds the backend directly.

## Required `.env`

Set the target group:

```env
DEMO_CHAT_ID=oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Optional role webhooks:

```env
DEMO_WEBHOOK_PM=
DEMO_WEBHOOK_DEV=
DEMO_WEBHOOK_HR=
DEMO_WEBHOOK_ALGO=
DEMO_WEBHOOK_DESIGN=
DEMO_WEBHOOK_QA=
DEMO_WEBHOOK_OPS=
```

Each webhook should be a Feishu custom bot installed in the same `DEMO_CHAT_ID` group.

## Run

Recommended demo run:

```powershell
python -m demo.play_feishu_demo --reset --message-delay 0.15 --batch-pause 1 --hide-role-label
```

Smoke test one batch:

```powershell
python -m demo.play_feishu_demo --reset --max-batches 1 --message-delay 0.3
```

Keep Graphiti card writes, closer to the slower main path:

```powershell
python -m demo.play_feishu_demo --keep-graphiti-card-write
```

## Query After Playback

Start the real OpenClaw bot service in another terminal:

```powershell
python main.py
```

After playback, mention the OpenClaw application bot in the same group. Queries use the memory already written under `DEMO_CHAT_ID`; they do not pull Feishu history on demand.

## Notes

- `--reset` clears memory for the demo group before playback.
- `--hide-role-label` is useful when separate custom bots already show the role by name/avatar.
- If a role webhook is missing, that role falls back to the OpenClaw app bot sender.
- Real human messages sent after the demo can still be written by `python main.py` through the normal polling path.
