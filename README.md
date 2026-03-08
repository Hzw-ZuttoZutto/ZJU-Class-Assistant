# Fuck-ZJU 使用门户

统一入口：

```bash
python -m src.main <subcommand> ...
```

## 1. 功能总览

面向日常使用，只保留 4 类核心功能：

- `scan`：按教师名 + 课程名扫描课程 ID。
- `watch`：直播代理播放（教师流/PPT 流）+ 录制 + 实时关键信息提取。
- `mic-listen` + `mic-publish`：独立麦克风采集链路（不依赖 watch）。
- `mic-list-devices`：列出本机可用麦克风设备。

## 2. 快速开始

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 复制账号模板并填写：

```bash
cp account .account
```

3. 直接运行（示例：扫描课程）：

```bash
python -m src.main scan --teacher '王强' --title '编译原理'
```

## 3. `.account` 填写说明

- 读取文件：仓库根目录 `.account`。
- 键名大小写不敏感（内部统一按小写解析）。
- 建议全部使用模板中的大写键名，便于团队统一。

### 3.1 字段说明

| 键名 | 必填 | 用于什么功能 | 含义 |
|---|---|---|---|
| `USERNAME` | 是（`scan/watch`） | `scan`、`watch` | 浙大统一认证账号 |
| `PASSWORD` | 是（`scan/watch`） | `scan`、`watch` | 浙大统一认证密码 |
| `OPENAI_API_KEY` | 与 `AIHUBMIX_API_KEY` 二选一 | `watch` / `mic-listen` 实时分析 | OpenAI 兼容文本/语音能力 Key |
| `AIHUBMIX_API_KEY` | 与 `OPENAI_API_KEY` 二选一 | `watch` / `mic-listen` 实时分析 | AIHubMix 网关 Key |
| `OPENAI_BASE_URL` | 否 | `watch` / `mic-listen` 实时分析 | OpenAI 兼容网关地址 |
| `DASHSCOPE_API_KEY` | stream 模式必填 | `watch` / `mic-listen` 的 `--rt-pipeline-mode stream` | DashScope 实时 ASR Key |
| `DINGTALK_WEBHOOK` | 开启告警时必填 | `watch` / `mic-listen` 的 `--rt-dingtalk-enabled` | 钉钉机器人 Webhook |
| `DINGTALK_SECRET` | 开启告警时必填 | `watch` / `mic-listen` 的 `--rt-dingtalk-enabled` | 钉钉机器人签名 Secret |

### 3.2 优先级规则

- 登录凭据：CLI `--username/--password` > `.account`。
- OpenAI/AIHubMix Key：`.account` > 环境变量。
- Base URL：`.account` > 环境变量。
- DashScope Key：`.account` > 环境变量。
- DingTalk 机器人：`.account` > 环境变量。
- 仅配置 `AIHUBMIX_API_KEY` 且未设置 Base URL 时，默认使用 `https://aihubmix.com/v1`。

## 4. 实践默认参数配置（可直接复制）

### 4.1 课程扫描（默认）

```bash
python -m src.main scan \
  --teacher '王强' \
  --title '编译原理' \
  --center 83650 \
  --radius 100 \
  --workers 64 \
  --retries 1 \
  --verbose
```

仅保留“直播中”课程：

```bash
python -m src.main scan \
  --teacher '王强' \
  --title '编译原理' \
  --center 83650 \
  --radius 100 \
  --require-live \
  --live-check-timeout 30 \
  --live-check-interval 2 \
  --workers 64
```

### 4.2 `watch` + 录制 + chunk 实时分析（实践默认）

```bash
python -m src.main watch \
  --course-id 83650 \
  --sub-id 1895397 \
  --poll-interval 3 \
  --port 8765 \
  --record-dir ./records \
  --record-segment-minutes 10 \
  --record-startup-av-timeout 15 \
  --record-recovery-window-sec 10 \
  --rt-insight-enabled \
  --rt-pipeline-mode chunk \
  --rt-stt-model whisper-large-v3 \
  --rt-model gpt-4.1-mini \
  --rt-api-base-url https://aihubmix.com/v1 \
  --rt-chunk-seconds 10 \
  --rt-context-window-seconds 180 \
  --rt-keywords-file config/realtime_keywords.json \
  --rt-stt-request-timeout-sec 8 \
  --rt-stt-stage-timeout-sec 32 \
  --rt-stt-retry-count 4 \
  --rt-stt-retry-interval-sec 0.2 \
  --rt-analysis-request-timeout-sec 15 \
  --rt-analysis-stage-timeout-sec 60 \
  --rt-analysis-retry-count 4 \
  --rt-analysis-retry-interval-sec 0.2 \
  --rt-alert-threshold 90 \
  --rt-dingtalk-enabled \
  --rt-dingtalk-cooldown-sec 30 \
  --rt-max-concurrency 5 \
  --rt-context-min-ready 15 \
  --rt-context-recent-required 4 \
  --rt-context-wait-timeout-sec-1 1 \
  --rt-context-wait-timeout-sec-2 5 \
  --no-browser
```

播放器地址：

- `http://127.0.0.1:8765/player?role=teacher`
- `http://127.0.0.1:8765/player?role=ppt`

### 4.3 `mic-listen(stream)` + `mic-publish(stream)`（实践默认）

1. 服务端启动：

```bash
TOKEN="micstream001"
SESSION_DIR="mic_session_$(date +%Y%m%d_%H%M%S)"

python -m src.main mic-listen \
  --host 127.0.0.1 \
  --port 18765 \
  --session-dir "$SESSION_DIR" \
  --mic-upload-token "$TOKEN" \
  --rt-pipeline-mode stream \
  --rt-dingtalk-enabled \
  --rt-dingtalk-cooldown-sec 0 \
  --rt-asr-scene zh \
  --rt-asr-model fun-asr-realtime \
  --rt-hotwords-file config/realtime_hotwords.json \
  --rt-window-sentences 8 \
  --rt-stream-analysis-workers 32 \
  --rt-stream-queue-size 100 \
  --rt-asr-endpoint wss://dashscope.aliyuncs.com/api-ws/v1/inference \
  --rt-chunk-seconds 10 \
  --rt-model gpt-4.1-mini \
  --rt-keywords-file config/realtime_keywords.json \
  --rt-analysis-request-timeout-sec 15 \
  --rt-analysis-stage-timeout-sec 60 \
  --rt-analysis-retry-count 4 \
  --rt-analysis-retry-interval-sec 0.2 \
  --rt-context-recent-required 4 \
  --rt-context-wait-timeout-sec-1 1 \
  --rt-context-wait-timeout-sec-2 5
```

2. 本机转发端口（如果 `mic-listen` 在远端机器）：

```bash
ssh -N -L 18765:127.0.0.1:18765 <your-server>
```

3. 本机查看设备并发布：

```bash
python -m src.main mic-list-devices

python -m src.main mic-publish \
  --target-url http://127.0.0.1:18765 \
  --mic-upload-token "$TOKEN" \
  --device "你的麦克风设备名" \
  --rt-pipeline-mode stream \
  --stream-frame-duration-ms 120 \
  --request-timeout-sec 20 \
  --retry-base-sec 1.0 \
  --retry-max-sec 12.0
```

## 5. 参数说明（按功能块）

### 5.1 `scan/watch` 通用登录参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--username` | 空 | 统一认证账号；不传则读 `.account` |
| `--password` | 空 | 统一认证密码；不传则读 `.account` |
| `--tenant-code` | `112` | 租户代码 |
| `--authcode` | 空 | 验证码（仅登录要求时填写） |
| `--timeout` | `20` | HTTP 超时秒数 |

### 5.2 `scan` 参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--teacher` | 必填 | 精确匹配教师名 |
| `--title` | 必填 | 精确匹配课程标题 |
| `--center` | `81889` | 扫描中心课程 ID |
| `--radius` | `200` | 扫描半径（即 `[center-radius, center+radius]`） |
| `--workers` | `min(64, max(4, cpu*2))` | 并发请求数 |
| `--retries` | `1` | 单请求失败重试次数 |
| `--verbose` | 关闭 | 输出每条被扫描课程 |
| `--require-live` | 关闭 | 仅保留“直播中”结果 |
| `--live-check-timeout` | `30.0` | 单候选课程直播状态最大等待秒数 |
| `--live-check-interval` | `2.0` | 直播状态轮询间隔秒数 |

### 5.3 `watch` 基础播放参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--course-id` | 必填 | 课程 ID |
| `--sub-id` | 必填 | 直播子 ID |
| `--poll-interval` | `10.0` | 上游流信息轮询间隔（秒） |
| `--host` | `127.0.0.1` | 本地服务监听地址 |
| `--port` | `8765` | 本地服务端口 |
| `--open-base-url` | 空 | 自动打开浏览器时使用的地址（端口映射场景） |
| `--no-browser` | 关闭 | 关闭自动拉起浏览器 |

### 5.4 `watch` 拉流容错参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--playlist-retries` | `3` | m3u8 拉取失败重试次数 |
| `--asset-retries` | `3` | 分片/密钥拉取失败重试次数 |
| `--stale-playlist-grace` | `15.0` | 上游失败时继续使用缓存 playlist 的秒数 |
| `--hls-max-buffer` | `20` | 浏览器端 HLS 缓冲长度参数 |

### 5.5 `watch` 录制参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--record-dir` | 空 | 录制根目录；不传则在当前目录创建会话目录 |
| `--record-segment-minutes` | `10` | 切片分钟数；`0` 表示整场一个文件 |
| `--record-startup-av-timeout` | `15.0` | 启动阶段等待音视频可用的最大秒数 |
| `--record-recovery-window-sec` | `10.0` | 断流恢复窗口；超过后记入缺失区间 |

### 5.6 `watch` 实时分析通用参数

> 只有开启 `--rt-insight-enabled` 后才生效。

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--rt-insight-enabled` | 关闭 | 开启实时关键信息提取 |
| `--rt-pipeline-mode` | `chunk` | 分析模式：`chunk` / `stream` |
| `--rt-model` | `gpt-4.1-mini` | 文本分析模型 |
| `--rt-keywords-file` | `config/realtime_keywords.json` | 关键词规则文件 |
| `--rt-api-base-url` | 空 | OpenAI 兼容网关地址 |
| `--rt-alert-threshold` | `90` | 触发 `[ALERT]` 的阈值 |
| `--rt-dingtalk-enabled` | 关闭 | 开启钉钉告警推送 |
| `--rt-dingtalk-cooldown-sec` | `30.0` | 钉钉告警冷却时间（秒） |

### 5.7 `watch` chunk 模式参数（`--rt-pipeline-mode chunk`）

> chunk 模式必须显式传 `--rt-stt-model`。

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--rt-stt-model` | 无 | 语音转写模型（必填） |
| `--rt-chunk-seconds` | `10` | 音频切片时长（秒） |
| `--rt-context-window-seconds` | `180` | 历史上下文窗口（秒） |
| `--rt-max-concurrency` | `5` | 并发处理 worker 数 |
| `--rt-stt-request-timeout-sec` | `8.0` | STT 单请求超时 |
| `--rt-stt-stage-timeout-sec` | `32.0` | STT 阶段总超时 |
| `--rt-stt-retry-count` | `4` | STT 阶段最大尝试次数 |
| `--rt-stt-retry-interval-sec` | `0.2` | STT 重试间隔 |
| `--rt-analysis-request-timeout-sec` | `15.0` | 分析单请求超时 |
| `--rt-analysis-stage-timeout-sec` | `60.0` | 分析阶段总超时 |
| `--rt-analysis-retry-count` | `4` | 分析阶段最大尝试次数 |
| `--rt-analysis-retry-interval-sec` | `0.2` | 分析重试间隔 |
| `--rt-context-min-ready` | `15` | 严格上下文门槛最小可用片段数 |
| `--rt-context-recent-required` | `4` | 最近必须可用片段数 |
| `--rt-context-wait-timeout-sec-1` | `1.0` | 最近片段齐备后额外等待时间 |
| `--rt-context-wait-timeout-sec-2` | `5.0` | 等待最近片段齐备的最大时长 |

### 5.8 `watch` stream 模式参数（`--rt-pipeline-mode stream`）

> stream 模式必须显式传 `--rt-asr-model`，并且必须启用 `--rt-dingtalk-enabled`。

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--rt-asr-scene` | `zh` | 实时 ASR 场景（`zh` / `multi`） |
| `--rt-asr-model` | 无 | 实时 ASR 模型（必填） |
| `--rt-hotwords-file` | `config/realtime_hotwords.json` | 热词 JSON 数组文件 |
| `--rt-window-sentences` | `8` | 句级滑窗大小 |
| `--rt-stream-analysis-workers` | `32` | 流式分析并发 worker 数 |
| `--rt-stream-queue-size` | `100` | 流式分析队列上限 |
| `--rt-asr-endpoint` | `wss://dashscope.aliyuncs.com/api-ws/v1/inference` | DashScope WebSocket 地址 |
| `--rt-translation-target-languages` | `zh` | 多语场景翻译目标语言（逗号分隔） |

补充约束：

- `DASHSCOPE_API_KEY` 必须可用（stream ASR 必需）。
- `--rt-hotwords-file` 必须是可读的 JSON 数组文件（`[]` 合法）。

### 5.9 `mic-listen` 基础参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `18765` | 监听端口 |
| `--session-dir` | 空 | 输出目录；不传则自动创建 `mic_session_<timestamp>` |
| `--mic-upload-token` | 空 | 上传令牌；也可用环境变量 `MIC_UPLOAD_TOKEN` |
| `--mic-chunk-max-bytes` | `10485760` | 单次上传最大字节数 |
| `--mic-chunk-dir` | `_rt_chunks_mic` | 接收切片目录（相对路径时挂到 `session-dir` 下） |

### 5.10 `mic-listen` 实时参数

`mic-listen` 的以下参数与 `watch` 同名同默认值、含义一致：

```text
--rt-pipeline-mode
--rt-chunk-seconds
--rt-context-window-seconds
--rt-model
--rt-stt-model
--rt-asr-scene
--rt-asr-model
--rt-hotwords-file
--rt-window-sentences
--rt-stream-analysis-workers
--rt-stream-queue-size
--rt-asr-endpoint
--rt-translation-target-languages
--rt-keywords-file
--rt-api-base-url
--rt-stt-request-timeout-sec
--rt-stt-stage-timeout-sec
--rt-stt-retry-count
--rt-stt-retry-interval-sec
--rt-analysis-request-timeout-sec
--rt-analysis-stage-timeout-sec
--rt-analysis-retry-count
--rt-analysis-retry-interval-sec
--rt-alert-threshold
--rt-dingtalk-enabled
--rt-dingtalk-cooldown-sec
--rt-context-min-ready
--rt-context-recent-required
--rt-context-wait-timeout-sec-1
--rt-context-wait-timeout-sec-2
```

`mic-listen` 额外参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--rt-profile-enabled` | 关闭 | 额外输出性能剖析日志 `realtime_profile.jsonl` |

模式约束：

- chunk 模式：必须显式传 `--rt-stt-model`。
- stream 模式：必须显式传 `--rt-asr-model` 且必须启用 `--rt-dingtalk-enabled`。

### 5.11 `mic-publish` 参数

| 参数 | 默认值 | 适用模式 | 含义 |
|---|---|---|---|
| `--target-url` | 必填 | 全部 | `mic-listen` 地址（如 `http://127.0.0.1:18765`） |
| `--mic-upload-token` | 必填 | 全部 | 与 `mic-listen` 一致的上传令牌 |
| `--device` | 必填 | 全部 | 麦克风设备名 |
| `--rt-pipeline-mode` | `chunk` | 全部 | 发布模式：`chunk` / `stream` |
| `--chunk-seconds` | `10.0` | chunk | 本地切片长度 |
| `--stream-frame-duration-ms` | `100` | stream | 每帧推送时长 |
| `--work-dir` / `--worker-dir` | 空 | chunk | 本地临时切片目录 |
| `--ffmpeg-bin` | 空 | 全部 | ffmpeg 路径；不传则走 PATH |
| `--request-timeout-sec` | `10.0` | 全部 | 请求超时 |
| `--ready-age-sec` | `1.2` | chunk | 文件稳定后再上传的等待时间 |
| `--retry-base-sec` | `0.5` | 全部 | 重试基准退避 |
| `--retry-max-sec` | `8.0` | 全部 | 重试最大退避 |
| `--scan-interval-sec` | `0.2` | chunk | 本地切片扫描周期 |

### 5.12 `mic-list-devices` 参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--ffmpeg-bin` | 空 | ffmpeg 路径；不传则走 PATH |

## 6. 主要输出文件

### 6.1 `watch` 会话目录

- `*.mp4` / `*.mp3`：录制切片。
- `*.missing.json`：单切片缺失区间。
- `recording_session_report.json`：会话级录制汇总。
- `realtime_transcripts.jsonl`：实时转写。
- `realtime_insights.jsonl`：结构化分析结果。
- `realtime_insights.log`：中文可读日志。
- `realtime_asr_events.jsonl`：stream 句级 ASR 事件。
- `analysis_prompt_trace.jsonl`：分析请求跟踪。

### 6.2 `mic-listen` 会话目录

- `realtime_transcripts.jsonl`
- `realtime_insights.jsonl`
- `realtime_insights.log`
- `realtime_asr_events.jsonl`（stream 模式）
- `realtime_dingtalk_trace.jsonl`（启用钉钉时）
- `realtime_profile.jsonl`（启用 `--rt-profile-enabled` 时）
