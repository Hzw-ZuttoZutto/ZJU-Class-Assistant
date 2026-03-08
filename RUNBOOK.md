# ZJU Classroom Live Tool Runbook

## 1. 目的与范围
本 Runbook 用于指导以下能力的日常运行与排障：

- 课程扫描（`scan`）
- 直播观看代理（`watch`，教师流 + PPT 流）
- 集群运行 + 本地浏览器访问（SSH 端口转发）

当前唯一入口为：

```bash
python -m src.main <subcommand> ...
```

Legacy script entrypoint has been removed; always use `python -m src.main <subcommand> ...`.

## 2. 项目结构（关键文件）
- `src/main.py`：总入口
- `src/cli/parser.py`：CLI 参数
- `src/auth/cas_client.py`：CAS 登录和 token 获取
- `src/scan/service.py`：课程扫描
- `src/live/server.py`：watch 服务入口
- `src/live/poller.py`：上游流轮询
- `src/live/providers/meta_provider.py`：传统 `getscreenstream/get-stream` 链路
- `src/live/providers/livingroom_provider.py`：`livingroom` 架构链路（`search-live-course-list + sub_content`）
- `src/live/proxy.py`：m3u8/分片代理与重试
- `src/live/templates.py`：控制台和播放器页面
- `src/live_video.py`：教师流选择策略（优先含音轨）
- `src/live_ppt.py`：PPT 流选择策略
- `tests/`：单元与集成测试

## 3. 运行前检查
在集群目录执行：

```bash
python --version
node --version
python -c "import requests; print(requests.__version__)"
```

建议先在工作区根目录创建 `.account`（避免命令行明文密码）：

```bash
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
```

说明：`scan/watch/simulate` 默认读取该文件；如同时传 `--username/--password`，则 CLI 参数优先。

建议版本（已验证）：

- Python 3.10+
- Node.js 20+
- requests 已安装

可选检查（代码语法/测试）：

```bash
python -m py_compile $(find src tests -name '*.py')
python -m unittest discover -s tests -v
```

## 4. 标准操作流程

### 4.1 课程扫描（scan）
用途：按 `teacher + title` 在 course_id 区间内查找课程。

示例：

```bash
python -m src.main scan \
  --teacher '王强' \
  --title '编译原理' \
  --center 83650 \
  --radius 0 \
  --workers 1 \
  --retries 1 \
  --verbose
```

仅保留“当前直播中”课程（可选）：

```bash
python -m src.main scan \
  --teacher '王强' \
  --title '编译原理' \
  --center 83650 \
  --radius 0 \
  --require-live \
  --live-check-timeout 30 \
  --live-check-interval 2
```

成功标志：

- 输出 JSON，`mode=scan`
- `matches` 中出现目标课程时即命中
- 开启 `--require-live` 后，失败候选会在 `live_check_failures` 返回，并在终端显示 `[LIVE-CHECK-FAIL]`

### 4.2 直播服务（watch）
用途：持续拉取上游直播流并提供本地代理播放。

示例：

```bash
python -m src.main watch \
  --course-id 83650 \
  --sub-id 1895397 \
  --host 127.0.0.1 \
  --port 8765 \
  --poll-interval 3 \
  --playlist-retries 3 \
  --asset-retries 3 \
  --stale-playlist-grace 15 \
  --hls-max-buffer 20 \
  --record-dir ./records \
  --record-segment-minutes 10 \
  --record-startup-av-timeout 15 \
  --record-recovery-window-sec 10 \
  --no-browser
```

开启实时关键信息提取（可选）：

```bash
python -m src.main watch \
  --course-id 83650 \
  --sub-id 1895397 \
  --record-dir ./records \
  # default balanced preset: gpt-4.1-mini + 10s chunk
  --rt-insight-enabled \
  --rt-dingtalk-enabled \
  --rt-dingtalk-cooldown-sec 30 \
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
  --rt-max-concurrency 5 \
  --rt-context-min-ready 15 \
  --rt-context-recent-required 4 \
  --rt-context-wait-timeout-sec-1 1 \
  --rt-context-wait-timeout-sec-2 5 \
  --no-browser
```

注意：

- `--sub-id` 必填；缺失时无法正确拉流。
- 打开 `/player` 页面后不会自动播放；需要点击页面中央“点击播放”按钮后开始播放。
- 点击播放后，播放器会自动重试并尽量恢复声音，不需要额外的声音开关操作。

启动后关键地址：

- 控制台：`http://127.0.0.1:8765/`
- 教师播放：`http://127.0.0.1:8765/player?role=teacher`
- PPT 播放：`http://127.0.0.1:8765/player?role=ppt`
- 指标：`http://127.0.0.1:8765/api/metrics`

停止方式：

- 前台运行：`Ctrl + C`
- 后台运行：`kill <PID>`

录制文件说明：

- 目录：`--record-dir` 不传则默认在当前工作目录创建会话文件夹。
- 会话目录命名：`课程名_老师名_watch启动时间`
- 分片命名：`课程名_老师名_开始时间_结束时间.mp4/.mp3`
- 缺失区间日志：每分片 `*.missing.json`，全局 `recording_session_report.json`
- `--record-segment-minutes 0` 表示整场只输出一个分片，直到手动终止。
- 实时转写日志：`realtime_transcripts.jsonl`
- 实时结构化日志：`realtime_insights.jsonl`
- 实时中文镜像日志：`realtime_insights.log`
- 分析 Prompt 调试日志：`analysis_prompt_trace.jsonl`
- 实时音频切片目录：`_rt_chunks/`
- 实时流程：`10s音频 -> STT转写 -> 文本上下文分析`
- 关键词配置默认使用 `config/realtime_keywords.json`，支持 `version: 2` 分组规则；新增事件分组只需追加 `groups` 项。
- 旧版 `important_terms/important_phrases/negative_terms` 配置仍兼容。
- 可选钉钉告警：通过 `--rt-dingtalk-enabled` 开启，仅转发 `important=true` 事件。
- 钉钉凭据读取：`.account` 中 `dingtalk_webhook` / `dingtalk_secret`，或环境变量 `DINGTALK_WEBHOOK` / `DINGTALK_SECRET`。

### 4.3 分析仿真器（simulate）
用途：对实时分析链路做可控离线仿真，覆盖 5 种模式。

目录约定（默认）：

- 输入音频目录：`tests/simulator/mp3_inputs/`
- 场景目录：`tests/simulator/scenarios/mode1..mode5/`
- 缓存目录：`tests/simulator/cache/stt` 与 `tests/simulator/cache/analysis`
- 运行输出：`tests/simulator/runs/`

示例（mode1 全流程仿真）：

```bash
python -m src.main simulate \
  --mode 1 \
  --scenario-file tests/simulator/scenarios/mode1/example.yaml \
  --mp3-dir tests/simulator/mp3_inputs \
  --run-dir tests/simulator/runs \
  --chunk-seconds 10
```

示例（mode2 翻译阶段可控，启动前全量预计算）：

```bash
python -m src.main simulate \
  --mode 2 \
  --scenario-file tests/simulator/scenarios/mode2/example.yaml \
  --rt-api-base-url https://aihubmix.com/v1 \
  --rt-stt-model whisper-large-v3 \
  --precompute-workers 4
```

示例（mode4 翻译 API 响应时间基准）：

```bash
python -m src.main simulate \
  --mode 4 \
  --scenario-file tests/simulator/scenarios/mode4/example.yaml \
  --rt-api-base-url https://aihubmix.com/v1 \
  --rt-stt-model whisper-large-v3
```

关键说明：

- 模式2/3会在仿真启动前全量预计算：缓存命中直接加载，未命中补算并写入缓存。
- 模式3历史可见性支持18位串控制：右侧最低位对应 `seq-1`，左侧最高位对应 `seq-18`。
- 模式4为 STT 基准测试：每次样本都直接请求 STT API，不用本地 STT 缓存替代。
- 模式5为分析基准测试：分析阶段不读取 analysis 缓存；分析前统一转写阶段允许命中 STT 缓存，缺失时补算并写回。
- 仿真输出目录会产出：
  - `realtime_transcripts.jsonl`
  - `realtime_insights.jsonl`
  - `realtime_insights.log`
  - `simulate_report.json`
  - `precompute_manifest.json`（仅mode2/mode3）

### 4.4 本地浏览器访问（SSH 转发）
在本地机器执行：

```bash
ssh <clusters> -L 8765:127.0.0.1:8765
```

然后在本地浏览器打开：

- `http://127.0.0.1:8765/player?role=teacher`
- `http://127.0.0.1:8765/player?role=ppt`

### 4.5 独立麦克风上游（mic-listen + mic-publish）
用途：不启动直播/录制，仅使用“本机麦克风 -> SSH 中继 -> 集群分析”链路。

1) 集群启动接收与分析服务：

```bash
python -m src.main mic-listen \
  --host 127.0.0.1 \
  --port 18765 \
  --mic-upload-token YOUR_TOKEN \
  # default balanced preset: gpt-4.1-mini + 10s chunk
  --rt-dingtalk-enabled \
  --rt-dingtalk-cooldown-sec 30 \
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
python -m src.main mic-publish \
  --target-url http://127.0.0.1:18765 \
  --mic-upload-token YOUR_TOKEN \
  --device "你的麦克风设备名" \
  --chunk-seconds 10
```

输出文件与 `watch --rt-insight-enabled` 相同，位于 `mic-listen` 的 `session_dir`：
- `realtime_transcripts.jsonl`
- `realtime_insights.jsonl`
- `realtime_insights.log`
- 若开启钉钉告警，默认 `30s` 冷却窗口内只接受第一条紧急提醒。

## 5. 指标说明（`/api/metrics`）

`poller` 字段：

- `poll_total`：轮询总次数
- `poll_failures`：轮询失败次数
- `consecutive_poll_failures`：连续轮询失败次数（应能恢复到 0）
- `last_error`：最近错误信息

`proxy.proxy` 字段：

- `playlist_requests/failures`：m3u8 请求/失败
- `playlist_stale_hits`：命中陈旧 playlist 兜底次数
- `asset_requests/failures`：分片请求/失败
- `asset_retry_successes`：分片经重试后成功次数
- `consecutive_asset_failures`：连续分片失败次数（应能恢复到 0）

`proxy.playlist_cache` 字段：

- 每个 role 的缓存年龄和大小

## 6. 快速命令速查

```bash
# 1) 扫描课程
python -m src.main scan --teacher '王强' --title '测试标题' --center 83650 --radius 0 --workers 1 --retries 1 --verbose

# 2) 启动直播代理
python -m src.main watch --course-id 83650 --sub-id <sub_id> --poll-interval 3 --port 8765 --no-browser

# 3) 查看指标
curl -s http://127.0.0.1:8765/api/metrics

# 4) 本地转发（在本地机器）
ssh <clusters> -L 8765:127.0.0.1:8765
```
