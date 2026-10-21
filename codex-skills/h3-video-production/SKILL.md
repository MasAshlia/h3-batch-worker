---
name: h3-video-production
description: Run local h3-batch-worker video production from Codex attachments. Use when the user supplies episode or shot prompts plus reference assets and asks to preflight, generate, monitor, or open the H3 production console. Do not use for writing cinematic prompts without running production.
---

# H3 Video Production

Use the bundled `scripts/h3_codex.py`; do not recreate asset staging or HTTP calls by hand.

## Setup

Run the bundled bridge by its absolute path. On a normal Windows installation it is `%USERPROFILE%\.codex\skills\h3-video-production\scripts\h3_codex.py`.

If the project is missing, run `py -3.12 <bridge-path> bootstrap`. This installs only h3-batch-worker and this skill. It must not install or upgrade ComfyUI, custom nodes, models, CUDA, or PyTorch.

## Submit

Resolve every attached Prompt and asset path first. Then run:

```powershell
py -3.12 <bridge-path> submit --prompt <prompt.txt> --asset <assets.zip> --run-name "A4 E15 HMS"
```

- Repeat `--prompt` and `--asset` for multiple inputs. Assets may be ZIP files, directories, PNG, JPG, JPEG, or WEBP files.
- Default to one generation per shot. Use `--count 2` for all shots or repeat `--shot-count S15-1=2` for overrides; only 1-4 are valid.
- When the user asks only to inspect, analyze, or confirm mappings, add `--preflight-only`. When they explicitly ask to generate or produce, submit immediately after a successful preflight.
- Preserve Prompt text. Never guess a missing or ambiguous Asset ID.

The command returns the preflight mappings, session ID, and `http://127.0.0.1:8765/`. Open that URL when useful.

## Monitor

Run `py -3.12 <bridge-path> status` and report completed/total shots, the active shot, failures, and produced videos. A received `prompt_id` must never be resubmitted automatically. Stop on `FAILED` or `UNKNOWN_SUBMISSION` and give the recorded diagnostic.
