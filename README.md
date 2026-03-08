# ZJU Classroom Live Tool

直播代理与课程扫描工具，统一入口为：

```bash
python -m src.main <subcommand> ...
```

## 目录

- `src/main.py`：CLI 总入口
- `src/scan/`：课程扫描逻辑
- `src/live/`：直播轮询、代理、Web 服务
- `src/simulator/`：实时分析仿真器（6模式）
- `src/live_video.py`：教师流选择策略（优先音轨可用）
- `src/live_ppt.py`：PPT 流选择策略
- `tests/`：单元与集成测试
- `RUNBOOK.md`：运行与排障手册

## 快速开始

```bash
# 安装依赖（realtime insight 需要 openai sdk）
pip install -r requirements.txt

# 凭据文件（推荐，避免在命令行暴露密码）
cat > .account <<'EOF'
USERNAME=你的统一认证账号
PASSWORD=你的统一认证密码
# 二选一即可：
# 1) OpenAI 官方 key
OPENAI_API_KEY=你的OpenAIKey
# 2) AIHubMix key（OpenAI 兼容网关）
# AIHUBMIX_API_KEY=你的AIHubMixKey
# OPENAI_BASE_URL=https://aihubmix.com/v1
# 可选：钉钉机器人告警
# DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=...
# DINGTALK_SECRET=SEC...
EOF

# 语法检查 + 测试
python -m py_compile $(find src tests -name '*.py')
python -m unittest discover -s tests -v

# 扫描课程
python -m src.main scan --teacher '王强' --title '编译原理' --center 83650 --radius 100 --workers 64 --retries 1 --verbose

# 扫描课程（仅保留“直播中”）
python -m src.main scan \
  --teacher '王强' \
  --title '编译原理' \
  --center 83650 \
  --radius 100 \
  --require-live \
  --live-check-timeout 30 \
  --live-check-interval 2 \
  --worker 64

# 启动直播代理
python -m src.main watch --course-id 83650 --sub-id <sub_id> --poll-interval 3 --port 8765 --no-browser

# 启动直播代理 + 录制（默认每10分钟切片；0=整场单文件）
python -m src.main watch \
  --course-id 83650 \
  --sub-id <sub_id> \
  --record-dir ./records \
  --record-segment-minutes 10 \
  --record-startup-av-timeout 15 \
  --record-recovery-window-sec 10

# 启动直播代理 + 录制 + 实时关键信息提取（10s 音频增量）
python -m src.main watch \
  --course-id 83650 \
  --sub-id <sub_id> \
  --record-dir ./records \
  # default balanced preset: gpt-4.1-mini + 10s chunk
  --rt-insight-enabled \
  --rt-dingtalk-enabled \
  --rt-dingtalk-cooldown-sec 30 \
  --rt-stt-model whisper-large-v3 \
  --rt-model gpt-4.1-mini \
  --rt-chunk-seconds 10 \
  --rt-context-window-seconds 180 \
  --rt-max-concurrency 5 \
  --rt-stt-request-timeout-sec 8 \
  --rt-stt-stage-timeout-sec 32 \
  --rt-stt-retry-count 4 \
  --rt-stt-retry-interval-sec 0.2 \
  --rt-analysis-request-timeout-sec 15 \
  --rt-analysis-stage-timeout-sec 60 \
  --rt-analysis-retry-count 4 \
  --rt-analysis-retry-interval-sec 0.2 \
  --rt-context-min-ready 15 \
  --rt-context-recent-required 4 \
  --rt-context-wait-timeout-sec-1 1 \
  --rt-context-wait-timeout-sec-2 5 \
  --rt-api-base-url https://aihubmix.com/v1 \
  --rt-keywords-file config/realtime_keywords.json

# 启动仿真器（mode1：全流程）
python -m src.main simulate \
  --mode 1 \
  --scenario-file tests/simulator/scenarios/mode1/example.yaml \
  --mp3-dir tests/simulator/mp3_inputs \
  --run-dir tests/simulator/runs \
  --chunk-seconds 10

# 启动仿真器（mode2：翻译可控，启动前全量预计算）
python -m src.main simulate \
  --mode 2 \
  --scenario-file tests/simulator/scenarios/mode2/example.yaml \
  --rt-api-base-url https://aihubmix.com/v1 \
  --rt-stt-model whisper-large-v3 \
  --precompute-workers 4

# 启动仿真器（mode4：翻译 API 响应时间基准）
python -m src.main simulate \
  --mode 4 \
  --scenario-file tests/simulator/scenarios/mode4/example.yaml \
  --rt-api-base-url https://aihubmix.com/v1 \
  --rt-stt-model whisper-large-v3

# 启动仿真器（mode6：离线逻辑正确性验证，不依赖 mp3 / OpenAI）
python -m src.main simulate \
  --mode 6 \
  --scenario-file tests/simulator/scenarios/mode6/example.yaml
```

凭据规则：

- 默认从工作区根目录 `.account` 读取 `USERNAME`、`PASSWORD` 和 AI 模型 key。
- 仍可通过 `--username/--password` 显式传入；CLI 传参优先级更高。
- AI key 读取优先级：
  - `.account` 中 `OPENAI_API_KEY` / `AIHUBMIX_API_KEY`
  - 环境变量 `OPENAI_API_KEY` / `AIHUBMIX_API_KEY`
- Base URL 可通过 `.account` 或环境变量中的 `OPENAI_BASE_URL` / `AIHUBMIX_BASE_URL` 指定。
- 若只配置 `AIHUBMIX_API_KEY` 且未显式给 Base URL，默认使用 `https://aihubmix.com/v1`。
- 钉钉机器人告警可从 `.account` 或环境变量读取：
  - `.account`: `dingtalk_webhook=...` 与 `dingtalk_secret=...`
  - 环境变量：`DINGTALK_WEBHOOK` 与 `DINGTALK_SECRET`

录制产物：

- 每段 `mp4`：`课程名_老师名_开始时间_结束时间.mp4`
- 每段 `mp3`：`课程名_老师名_开始时间_结束时间.mp3`
- 每段缺失日志：`课程名_老师名_开始时间_结束时间.missing.json`
- 会话汇总：`recording_session_report.json`
- 实时转写日志：`realtime_transcripts.jsonl`
- 实时结构化日志：`realtime_insights.jsonl`
- 实时中文镜像日志：`realtime_insights.log`

实时提取说明：

- 必须提供 `OPENAI_API_KEY`（推荐写在 `.account`）。
- 关键词默认文件：`config/realtime_keywords.json`。
- 实时流程为两阶段：`10s音频 -> STT转写 -> 文本上下文分析`。
- 紧急度为二分类：重要=95%，非重要或失败降级=10%。
- 可选钉钉告警：仅 `watch` / `mic-listen` 支持，通过 `--rt-dingtalk-enabled` 开启。
- 钉钉告警冷却时间默认 `30s`，可通过 `--rt-dingtalk-cooldown-sec` 调整。

仿真器说明：

- 场景文件格式为 YAML，目录按模式组织：`tests/simulator/scenarios/mode1..mode6/`。
- 模式2/3会在仿真启动前执行全量预计算：缓存命中直接复用，未命中补算并写入 `tests/simulator/cache/{stt,analysis}`。
- 模式3支持 18 位历史可见性串控制：右侧最低位对应 `seq-1`，左侧最高位对应 `seq-18`。
- 模式4为 STT 基准测试：每次样本都直接请求 STT API，不用本地 STT 缓存替代。
- 模式5为分析基准测试：分析阶段不读取 analysis 缓存；分析前统一转写阶段允许命中 STT 缓存，缺失时补算并写回。
- 模式6为离线逻辑验证：按 `mode6.cases` 脚本驱动 STT/历史到达并做严格断言，输出 `mode6_report.json` 与 `mode6_trace.jsonl`。
- 仿真运行产物位于 `tests/simulator/runs/<scenario>_modeX_<ts>/`，核心文件：
  - `realtime_transcripts.jsonl`
  - `realtime_insights.jsonl`
  - `realtime_insights.log`
  - `simulate_report.json`
  - `precompute_manifest.json`（仅mode2/mode3）

本地浏览器观看（本地执行）：

```bash
ssh <clusters> -L 8765:127.0.0.1:8765
```

然后打开：

- `http://127.0.0.1:8765/player?role=teacher`
- `http://127.0.0.1:8765/player?role=ppt`

独立麦克风上游（`mic-listen + mic-publish`）：

- 用途：不启动直播/录制，仅使用“本机麦克风 -> SSH 中继 -> 集群分析”链路。

1) 集群启动接收与分析服务：

```bash
python -m src.main mic-listen \
  --host 127.0.0.1 \
  --port 18765 \
  --mic-upload-token YOUR_TOKEN \
  --rt-dingtalk-enabled \
  --rt-dingtalk-cooldown-sec 30 \
  # default balanced preset: gpt-4.1-mini + 10s chunk
  --rt-chunk-seconds 10 \
  --rt-stt-model whisper-large-v3 \
  --rt-model gpt-4.1-mini \
  --rt-keywords-file config/realtime_keywords.json \
  --rt-stt-request-timeout-sec 8 \
  --rt-stt-stage-timeout-sec 32 \
  --rt-stt-retry-count 4 \
  --rt-stt-retry-interval-sec 0.2 \
  --rt-analysis-request-timeout-sec 15 \
  --rt-analysis-stage-timeout-sec 60 \
  --rt-analysis-retry-count 4 \
  --rt-analysis-retry-interval-sec 0.2 \
  --rt-context-recent-required 4 \
  --rt-context-wait-timeout-sec-1 1 \
  --rt-context-wait-timeout-sec-2 5
```

2) 本机（Windows）建立 SSH 端口转发：

```bash
ssh <cluster> -L 18765:127.0.0.1:18765
```

3) 本机查看麦克风设备：

```bash
python -m src.main mic-list-devices
```

4) 本机启动麦克风发布：

```bash
python -m src.main mic-publish --target-url http://127.0.0.1:18765 --mic-upload-token YOUR_TOKEN --device "你的麦克风设备" --chunk-seconds 10
```

输出文件与 `watch --rt-insight-enabled` 相同，位于 `mic-listen` 的 `session_dir`：

- `realtime_transcripts.jsonl`
- `realtime_insights.jsonl`
- `realtime_insights.log`
- 若开启钉钉告警，只会转发 `important=true` 的事件，且 `30s` 冷却窗口内的新紧急事件会直接丢弃。
