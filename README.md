# Fuck-ZJU 使用门户

## 项目定位与核心功能

项目定位：

- 面向“上课实用场景”的课堂直播辅助工具，聚焦“快速找到课程、稳定获取直播音频、实时提取紧急事项并通知”。

核心功能：

- 课程扫描：按教师名与课程名在课程 ID 区间内快速定位目标课程。
- 直播流实时分析：从课堂直播拉取教师音频流，执行 stream ASR + 关键词分析 + 钉钉告警。
- 独立麦克风链路：支持 `mic-listen + mic-publish` 在无 analysis 场景下单独运行实时分析。

统一入口：

```bash
python -m src.main <subcommand> ...
```

## 1. 功能总览

面向日常使用，只保留 4 类核心功能：

- `scan`：按教师名 + 课程名扫描课程 ID。
- `analysis`：直播音频流实时分析（stream ASR + 关键词提炼 + 可选钉钉告警）。
- `mic-listen` + `mic-publish`：独立麦克风采集链路（不依赖 analysis）。
- `mic-list-devices`：列出本机可用麦克风设备。

## 2. 快速开始

1. 安装依赖：

```bash
conda create -n fuckclass python=3.9
pip install -r requirements.txt
sudo apt update && sudo apt install -y ffmpeg
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
| `USERNAME` | 是（`scan/analysis`） | `scan`、`analysis` | 浙大统一认证账号 |
| `PASSWORD` | 是（`scan/analysis`） | `scan`、`analysis` | 浙大统一认证密码 |
| `OPENAI_API_KEY` | 与 `AIHUBMIX_API_KEY` 二选一 | `analysis` / `mic-listen` 实时分析 | OpenAI 兼容文本/语音能力 Key |
| `AIHUBMIX_API_KEY` | 与 `OPENAI_API_KEY` 二选一 | `analysis` / `mic-listen` 实时分析 | AIHubMix 网关 Key |
| `OPENAI_BASE_URL` | 否 | `analysis` / `mic-listen` 实时分析 | OpenAI 兼容网关地址 |
| `DASHSCOPE_API_KEY` | stream 模式必填 | `analysis` / `mic-listen(stream)` | DashScope 实时 ASR Key |
| `DINGTALK_WEBHOOK` | 开启告警时必填 | `analysis` / `mic-listen` 的 `--rt-dingtalk-enabled` | 钉钉机器人 Webhook |
| `DINGTALK_SECRET` | 开启告警时必填 | `analysis` / `mic-listen` 的 `--rt-dingtalk-enabled` | 钉钉机器人签名 Secret |

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
  --teacher '章献民' \
  --title '信息与电子工程导论' \
  --center 81975 \
  --radius 100 \
  --require-live \
  --live-check-timeout 30 \
  --live-check-interval 2 \
  --workers 64
```

### 4.2 `analysis` + stream 实时分析（实践默认）

```bash
python -m src.main analysis \
  --course-id 81975 \
  --sub-id 1896537 \
  --poll-interval 3 \
  --output-dir ./records \
  --rt-model gpt-4.1-mini \
  --rt-asr-scene zh \
  --rt-asr-model fun-asr-realtime \
  --rt-hotwords-file config/realtime_hotwords.json \
  --rt-window-sentences 8 \
  --rt-stream-analysis-workers 32 \
  --rt-stream-queue-size 100 \
  --rt-asr-endpoint wss://dashscope.aliyuncs.com/api-ws/v1/inference \
  --rt-api-base-url https://aihubmix.com/v1 \
  --rt-keywords-file config/realtime_keywords.json \
  --rt-analysis-request-timeout-sec 15 \
  --rt-analysis-stage-timeout-sec 60 \
  --rt-analysis-retry-count 4 \
  --rt-analysis-retry-interval-sec 0.2 \
  --rt-alert-threshold 90 \
  --rt-dingtalk-enabled \
  --rt-dingtalk-cooldown-sec 30 \
  --rt-context-recent-required 4 \
  --rt-context-wait-timeout-sec-1 1 \
  --rt-context-wait-timeout-sec-2 5
```

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

### 5.1 `scan/analysis` 通用登录参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--username` | 空 | 统一认证账号；不传则读 `.account` |
| `--password` | 空 | 统一认证密码；不传则读 `.account` |
| `--tenant-code` | `112` | 租户代码 |
| `--authcode` | 空 | 验证码（仅登录要求时填写） |
| `--timeout` | `20` | HTTP 超时秒数 |

补充说明：

- `scan/analysis` 的 CAS 登录阶段默认忽略环境代理（`http_proxy`/`https_proxy` 等），一般无需再手动 `env -u ...`。

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

### 5.3 `analysis` 基础参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--course-id` | 必填 | 课程 ID |
| `--sub-id` | 必填 | 直播子 ID |
| `--poll-interval` | `10.0` | 上游流信息轮询间隔（秒） |
| `--output-dir` | 空 | 输出根目录；不传则在当前目录创建会话目录 |

### 5.4 `analysis` stream 实时参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--rt-model` | `gpt-4.1-mini` | 文本分析模型 |
| `--rt-asr-scene` | `zh` | 实时 ASR 场景（`zh` / `multi`） |
| `--rt-asr-model` | 无 | 实时 ASR 模型（必填） |
| `--rt-hotwords-file` | `config/realtime_hotwords.json` | 热词 JSON 数组文件 |
| `--rt-window-sentences` | `8` | 句级滑窗大小 |
| `--rt-stream-analysis-workers` | `32` | 流式分析并发 worker 数 |
| `--rt-stream-queue-size` | `100` | 流式分析队列上限 |
| `--rt-asr-endpoint` | `wss://dashscope.aliyuncs.com/api-ws/v1/inference` | DashScope WebSocket 地址 |
| `--rt-translation-target-languages` | `zh` | 多语场景翻译目标语言（逗号分隔） |
| `--rt-keywords-file` | `config/realtime_keywords.json` | 关键词规则文件 |
| `--rt-api-base-url` | 空 | OpenAI 兼容网关地址 |
| `--rt-analysis-request-timeout-sec` | `15.0` | 分析单请求超时 |
| `--rt-analysis-stage-timeout-sec` | `60.0` | 分析阶段总超时 |
| `--rt-analysis-retry-count` | `4` | 分析阶段最大尝试次数 |
| `--rt-analysis-retry-interval-sec` | `0.2` | 分析重试间隔 |
| `--rt-alert-threshold` | `90` | 触发 `[ALERT]` 的阈值 |
| `--rt-dingtalk-enabled` | 关闭 | 开启钉钉告警推送 |
| `--rt-dingtalk-cooldown-sec` | `30.0` | 钉钉告警冷却时间（秒） |
| `--rt-context-recent-required` | `4` | 最近必须可用片段数 |
| `--rt-context-wait-timeout-sec-1` | `1.0` | 最近片段齐备后额外等待时间 |
| `--rt-context-wait-timeout-sec-2` | `5.0` | 等待最近片段齐备的最大时长 |

补充约束：

- `DASHSCOPE_API_KEY` 必须可用（stream ASR 必需）。
- `--rt-hotwords-file` 必须是可读的 JSON 数组文件（`[]` 合法）。
- `--rt-dingtalk-enabled` 可选，不开启时只写本地日志，不发钉钉。

### 5.5 `mic-listen` 基础参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `18765` | 监听端口 |
| `--session-dir` | 空 | 输出目录；不传则自动创建 `mic_session_<timestamp>` |
| `--mic-upload-token` | 空 | 上传令牌；也可用环境变量 `MIC_UPLOAD_TOKEN` |
| `--mic-chunk-max-bytes` | `10485760` | 单次上传最大字节数 |
| `--mic-chunk-dir` | `_rt_chunks_mic` | 接收切片目录（相对路径时挂到 `session-dir` 下） |

### 5.6 `mic-listen` 实时参数

`mic-listen` 与 `analysis` 共享的 stream 分析参数如下（`mic-listen` 还额外支持 chunk 相关参数）：

```text
--rt-model
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
--rt-analysis-request-timeout-sec
--rt-analysis-stage-timeout-sec
--rt-analysis-retry-count
--rt-analysis-retry-interval-sec
--rt-alert-threshold
--rt-dingtalk-enabled
--rt-dingtalk-cooldown-sec
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

### 5.7 `mic-publish` 参数

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

### 5.8 `mic-list-devices` 参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--ffmpeg-bin` | 空 | ffmpeg 路径；不传则走 PATH |

## 6. 主要输出文件

### 6.1 `analysis` 会话目录

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

## 7. 自定义关键词与热词

这一节用于按你的业务需求，修改：

- `config/realtime_keywords.json`：自定义实时分析规则（等价于“规则化 prompt 配置”）与紧急关键词。
- `config/realtime_hotwords.json`：自定义 stream ASR 的转写/翻译热词。

### 7.1 两个配置分别影响什么

| 文件 | 生效范围 | 作用 |
|---|---|---|
| `config/realtime_keywords.json` | `analysis`、`mic-listen`（chunk/stream 都会用） | 注入分析规则，影响 `important` 判定、`event_type` 与告警内容 |
| `config/realtime_hotwords.json` | `analysis`、`mic-listen --rt-pipeline-mode stream` | 传给 DashScope 实时 ASR，提升指定词的识别/翻译命中率 |

注意：

- 修改任一配置后，都需要重启 `analysis` 或 `mic-listen` 才会生效。
- `realtime_keywords.json` 配的是“规则内容”（会进入分析提示词）；系统模板文案在代码中固定。
- 若要直接改系统模板文案，请修改 `src/live/insight/prompting.py` 中的 `build_system_prompt`。

### 7.2 自定义 `realtime_keywords.json`（紧急关键词 + 规则 prompt）

推荐使用当前默认的 `version: 2` 分组格式：

| 字段 | 含义 | 修改建议 |
|---|---|---|
| `global_negative_terms` | 全局负向词（命中时倾向降权） | 放“闲聊/测试/无关口头语”等非紧急内容 |
| `groups[].id` | 事件类型唯一标识 | 使用英文短 id，如 `exam_notice` |
| `groups[].label` | 事件类型显示名 | 用中文业务名，如“考试通知” |
| `groups[].aliases` | 同义触发词 | 放常见关键词、同义词、缩写 |
| `groups[].phrases` | 典型完整短语 | 放老师常说的完整表达 |
| `groups[].detail_cues` | 延续细节线索 | 放题号/截止时间/链接/口令等执行细节词 |

新增一个事件分组（示例）：

```json
{
  "id": "exam_notice",
  "label": "考试通知",
  "aliases": ["考试", "考试安排", "期中考试", "期末考试"],
  "phrases": ["下周进行期中考试", "考试时间有调整"],
  "detail_cues": ["考试时间", "考试地点", "考试范围", "开卷", "闭卷"]
}
```

删除一个事件分组：

1. 在 `groups` 中删除对应 `id` 的对象。
2. 如该类词也出现在 `realtime_hotwords.json`，按需同步删除。

修改一个事件分组：

1. 保持 `id` 不变（避免事件类型漂移）。
2. 按业务迭代更新 `aliases`/`phrases`/`detail_cues`。
3. 高频误报词加入 `global_negative_terms`。

兼容说明：

- 旧版字段 `important_terms` / `important_phrases` / `negative_terms` 仍兼容。
- 但新配置建议统一用 `version: 2 + groups`，更容易按业务扩展。

### 7.3 自定义 `realtime_hotwords.json`（stream 转写/翻译热词）

文件格式必须是 JSON 字符串数组，例如：

```json
[
  "签到",
  "签到码",
  "作业提交",
  "截止时间",
  "期中考试"
]
```

增删改规则：

1. 新增热词：直接追加一个字符串元素。
2. 删除热词：删除对应字符串元素。
3. 修改热词：直接改字符串内容，尽量贴近教师真实说法。

注意：

- 根节点必须是数组；不是数组会导致 stream 模式启动失败。
- 文件不可读或 JSON 非法，也会导致 stream 模式启动失败。
- 空数组 `[]` 合法，但会失去热词增强效果。

### 7.4 校验与生效步骤

1. 校验 JSON 语法：

```bash
python -m json.tool config/realtime_keywords.json > /dev/null
python -m json.tool config/realtime_hotwords.json > /dev/null
```

2. 重启 `analysis` 或 `mic-listen`。

3. 观察日志确认加载成功：

- `keywords` 文件异常时，会打印 `using empty rules`（流程不崩，但规则失效）。
- stream 成功加载热词时，会打印 `loaded hotwords file ... items=<N>`。
