# Realtime Profile Analysis Report

Last updated: 2026-03-07

## 1. Scope

This report consolidates:

- model-request compatibility updates
- chunk-size sweep results (`5/8/10/15/20`)
- 7-model coverage benchmark
- two isolated metrics (no weighted merge)
- tuning guidance

## 2. Compatibility Updates

Files:

- `src/live/insight/openai_client.py`
- `src/live/mic.py`
- `src/cli/parser.py`

Implemented:

- Model-specific request payload branch:
  - `gpt-4.1*`: `text.verbosity=medium`, no default `reasoning` block
  - `gpt-5*`: `text.verbosity=low`, `reasoning.effort=minimal`
- Unsupported-value fallback:
  - auto-adjusts known fields (for example `text.verbosity`) when provider returns `Unsupported value ... Supported values are ...`
- Decimal chunk support:
  - `mic-listen --rt-chunk-seconds` supports float
  - `mic-publish --chunk-seconds` supports float

## 3. Metric Definitions (Separated)

Important: these two metrics are evaluated independently and never merged into one weighted score.

### 3.1 Metric A: Unit Audio-Second Analysis Cost

- Field used: `analysis_ms_per_audio_sec`
- Definition:
  - `analysis_ms_per_audio_sec = analysis_round_trip_ms / chunk_seconds`
- Interpretation:
  - smaller is better
  - represents analysis compute cost per second of audio

### 3.2 Metric B: Longest Analysis Waiting Time

This metric targets your exact concern:
if an important event appears at the very start of a chunk, how long until analysis result is ready.

- Per-chunk derived metric:
  - `event_start_wait_ms = chunk_seconds * 1000 + remote_total_ms`
- Reported value:
  - `longest_wait_ms_max = max(event_start_wait_ms)`
- Interpretation:
  - smaller is better
  - this is a pure worst-case freshness metric for chunk-start events

## 4. Experiment Setup

- Local: Windows mic publisher (`dshow`)
- Remote: `mic-listen` on clusters
- Transport: `ssh -L 18765:127.0.0.1:18765`
- Models (7):
  - `gpt-5-mini`
  - `gpt-5-nano`
  - `gpt-4.1-mini`
  - `gpt-4.1`
  - `gpt-4o-mini`
  - `gpt-4o`
  - `gpt-4.1-nano`
- Chunk sizes:
  - `5/8/10/15/20` seconds
- Common runtime knobs:
  - `--rt-context-window-seconds 60`
  - `--rt-context-min-ready 0`
  - `--rt-context-recent-required 1`
  - `--rt-context-wait-timeout-sec-1 0`
  - `--rt-context-wait-timeout-sec-2 0`
  - `--rt-stt-retry-count 2`
  - `--rt-analysis-retry-count 2`

## 5. Results

### 5.1 Metric A: Unit Audio-Second Analysis Cost

Value format: `avg (p95)` in `ms/s`.

| Model | 5s | 8s | 10s | 15s | 20s |
|---|---:|---:|---:|---:|---:|
| gpt-4.1 | 353.2 (p95 456.9) | 286.1 (p95 350.5) | 176.7 (p95 223.5) | 111.2 (p95 128.2) | 86.9 (p95 100.0) |
| gpt-4.1-mini | 256.7 (p95 293.0) | 152.2 (p95 159.2) | 128.3 (p95 158.0) | 91.3 (p95 104.4) | 90.2 (p95 130.6) |
| gpt-4.1-nano | 190.7 (p95 201.9) | 124.0 (p95 130.6) | 98.9 (p95 102.5) | 63.7 (p95 67.9) | 46.9 (p95 48.4) |
| gpt-4o | 615.6 (p95 746.4) | 380.6 (p95 439.7) | 316.2 (p95 359.9) | 240.4 (p95 286.6) | 159.8 (p95 200.0) |
| gpt-4o-mini | 1034.0 (p95 1233.0) | 650.5 (p95 710.2) | 481.0 (p95 600.3) | 400.6 (p95 421.8) | 228.8 (p95 288.2) |
| gpt-5-mini | 881.0 (p95 1208.6) | 633.1 (p95 749.9) | 473.9 (p95 497.1) | 305.0 (p95 348.1) | 256.0 (p95 353.8) |
| gpt-5-nano | 577.5 (p95 637.7) | 402.4 (p95 462.9) | 307.0 (p95 367.0) | 192.6 (p95 240.5) | 160.7 (p95 199.8) |

Per-chunk best model (Metric A):

- `5s`: `gpt-4.1-nano` (`190.7 ms/s`)
- `8s`: `gpt-4.1-nano` (`124.0 ms/s`)
- `10s`: `gpt-4.1-nano` (`98.9 ms/s`)
- `15s`: `gpt-4.1-nano` (`63.7 ms/s`)
- `20s`: `gpt-4.1-nano` (`46.9 ms/s`)

### 5.2 Metric B: Longest Analysis Waiting Time

Value format: `max (p95)` in `ms`.

| Model | 5s | 8s | 10s | 15s | 20s |
|---|---:|---:|---:|---:|---:|
| gpt-4.1 | 8328 (p95 8237) | 12592 (p95 12355) | 14620 (p95 14347) | 18735 (p95 18731) | 23411 (p95 23410) |
| gpt-4.1-mini | 7533 (p95 7389) | 10806 (p95 10769) | 13449 (p95 13337) | 17900 (p95 17851) | 24058 (p95 23888) |
| gpt-4.1-nano | 7382 (p95 7206) | 10912 (p95 10852) | 13290 (p95 13277) | 17834 (p95 17726) | 22573 (p95 22540) |
| gpt-4o | 9742 (p95 9663) | 12948 (p95 12778) | 14639 (p95 14581) | 20378 (p95 20352) | 25635 (p95 25520) |
| gpt-4o-mini | 14469 (p95 14090) | 14478 (p95 14443) | 16716 (p95 16715) | 22398 (p95 22348) | 27522 (p95 27417) |
| gpt-5-mini | 13703 (p95 13664) | 14702 (p95 14685) | 15952 (p95 15914) | 21356 (p95 21242) | 28427 (p95 28066) |
| gpt-5-nano | 9957 (p95 9627) | 13602 (p95 13552) | 15138 (p95 15037) | 20197 (p95 20118) | 26530 (p95 26364) |

Per-chunk best model (Metric B):

- `5s`: `gpt-4.1-nano` (`7382 ms`)
- `8s`: `gpt-4.1-mini` (`10806 ms`)
- `10s`: `gpt-4.1-nano` (`13290 ms`)
- `15s`: `gpt-4.1-nano` (`17834 ms`)
- `20s`: `gpt-4.1-nano` (`22573 ms`)

## 6. Tuning Guidance

Choose by objective, not by weighted merge:

1. If you prioritize compute efficiency (`analysis_ms_per_audio_sec`):
   - use larger chunks (`15s` or `20s`)
   - preferred models from this run: `gpt-4.1-nano`, then `gpt-4.1-mini`
2. If you prioritize worst-case freshness for chunk-start critical events (`longest_wait_ms_max`):
   - reduce chunk size first (`5s` gives the biggest gain)
   - preferred models from this run: `gpt-4.1-nano` / `gpt-4.1-mini`
3. If you need a balanced practical preset:
   - start with `chunk=8s` + `gpt-4.1-mini`
   - if still too slow on freshness, move to `chunk=5s`
   - if cost is too high, move to `chunk=10s`

## 7. Repro Notes

- Keep `--work-dir` unique per run to avoid stale-chunk replay.
- When comparing models, keep all non-model runtime knobs fixed.
- Raw per-run artifacts are under `.tmp_e2e_profiles/`:
  - `*.profile.jsonl`
  - `*.insights.jsonl`
  - `*.transcripts.jsonl`
  - aggregate CSV: `.tmp_e2e_profiles/chunk_model_report.csv`
