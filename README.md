# ZJU-Class-Assistant

ZJU-Class-Assistant 用于监听浙江大学课程直播中的课堂通知，并将识别到的作业、小测、调课、考试安排等信息推送到钉钉。

程序会登录课程系统、定位课程直播、读取直播音频，并通过 ASR 和 LLM 做实时转写与事件判断。也可以不接入课程直播，使用本机麦克风链路调试 ASR、关键词和提示词配置。

> 本项目仅用于个人学习和提醒。请遵守课程要求、平台规则和隐私边界；课程信息以教师、课程平台和学校通知为准。

## 功能

- `scan`：按教师名和课程名扫描课程 ID，并可筛选正在直播的课程。
- `analysis`：接入指定课程直播，进行实时转写、事件分析和钉钉提醒。
- `auto-analysis`：按 JSON 课表自动启动和停止多门课程的分析任务。
- `mic-listen` / `mic-publish`：不依赖课程直播，使用本机麦克风测试实时分析链路。
- `tingwu-process`：对 `analysis` 生成的听悟任务做课后转写和摘要后处理。

统一入口：

```bash
python -m src.main <subcommand> ...
```

## 最快路径

### 课程直播路径

1. 在 Linux / WSL / 服务器环境安装 Python 依赖、Node.js 和 ffmpeg。
2. 复制 `account.example` 为 `.account`，填写登录账号、ASR、LLM 和钉钉机器人配置。
3. 用 `scan` 找到目标课程的 `course_id` / `sub_id`。
4. 用 `analysis` 启动实时分析。
5. 看到程序创建会话目录并持续写入 `realtime_*.jsonl` / `realtime_insights.log`，说明链路已经启动；按 `Ctrl+C` 停止。

### 麦克风调试路径

1. 准备 ASR、LLM 和钉钉机器人配置。
2. 启动 `mic-listen` 服务端。
3. 在采集端用 `mic-list-devices` 查看设备名，再用 `mic-publish` 推送麦克风音频。
4. 观察会话目录中的转写、分析日志和钉钉提醒。

## 运行环境

推荐环境：

- Python 3.10+
- Node.js，CAS 登录阶段会调用 `node -e` 做密码加密
- `ffmpeg` / `ffprobe`
- Linux、WSL 或服务器环境用于长期运行 `analysis` / `auto-analysis`

平台说明：

- `auto-analysis` 使用 `fcntl` 文件锁，不支持原生 Windows。
- `mic-publish` 和 `mic-list-devices` 当前使用 Windows `dshow` 采集麦克风设备。
- 如果需要长期跑课程监听，建议把服务端放在 Linux / WSL / 服务器上；麦克风采集端可单独在 Windows 上运行。

安装依赖：

Linux / WSL：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

sudo apt update
sudo apt install -y ffmpeg nodejs
```

Windows 麦克风采集端：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Windows 采集端还需要安装 ffmpeg，并确保 `ffmpeg` 在 `PATH` 中。

## 配置账号和密钥

复制模板：

```bash
cp account.example .account
```

`.account` 是本地私密文件，已经被 `.gitignore` 忽略，不要提交。

常用字段：

| 字段                                       | 用途                                                                                  |
| ------------------------------------------ | ------------------------------------------------------------------------------------- |
| `USERNAME` / `PASSWORD`                | 浙大统一认证登录                                                                      |
| `OPENAI_API_KEY` 或 `AIHUBMIX_API_KEY` | 实时事件分析                                                                          |
| `OPENAI_BASE_URL`                        | OpenAI 兼容网关地址，可选；使用官方 OpenAI Key 时通常留空                             |
| `ZAI_API_KEY` / `GLM_API_KEY`          | 使用`glm-*` 模型时需要                                                              |
| `QWEN_API_KEY` | 使用 `qwen*` 分析模型时优先读取；未配置时可复用 `DASHSCOPE_API_KEY` |
| `QWEN_BASE_URL` | Qwen 分析接口，可选；默认 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `DASHSCOPE_API_KEY`                      | DashScope 流式 ASR                                                                    |
| `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` | 钉钉机器人提醒；`analysis`、`auto-analysis` 和 stream `mic-listen` 当前要求启用 |
| `ALIBABA_CLOUD_ACCESS_KEY_ID` 等         | 启用`--tingwu-enabled` 时需要                                                       |

Qwen 分析可设置 `rt_model` 为 `qwen3.7-flash`，`rt_api_base_url` 为 `https://dashscope.aliyuncs.com/compatible-mode/v1`。通过 Chat Completions 请求 JSON 输出，并设置 `enable_thinking=false`。其他地域或自定义网关需填写对应地址和凭据；原配置残留的 AIHubMix 地址会在 Qwen 路由中被忽略。分析使用独立的 `QWEN_API_KEY` 时不会修改 ASR 密钥。

不同场景的最小配置：

| 场景                 | 需要填写                                                 |
| -------------------- | -------------------------------------------------------- |
| `scan`             | `USERNAME` / `PASSWORD`                              |
| `analysis`         | 登录账号、`DASHSCOPE_API_KEY`、LLM Key、钉钉机器人配置 |
| `auto-analysis`    | 与`analysis` 相同，另需课表 JSON                       |
| stream`mic-listen` | `DASHSCOPE_API_KEY`、LLM Key、钉钉机器人配置           |
| `--tingwu-enabled` | 阿里云访问密钥、听悟 AppKey、OSS Bucket/Region/Endpoint  |

## 基本用法

### 1. 扫描课程

先根据教师名和课程名找到课程 ID：

```bash
python -m src.main scan \
  --teacher "教师姓名" \
  --title "课程名称" \
  --center 82000 \
  --radius 10000 \
  --require-live \
  --workers 64
```

输出中的 `course_id` 和 `sub_id` 可用于后续 `analysis`。

参数说明：

- `--center` 是扫描中心课程 ID。
- `--radius` 是向前后扩展的扫描范围。
- `--require-live` 只保留正在直播的结果；如果不是上课时间，可以先去掉它确认课程是否能被找到。

### 2. 分析一门正在直播的课程

最小常用命令：

```bash
python -m src.main analysis \
  --course-id <course_id> \
  --sub-id <sub_id> \
  --output-dir ./records \
  --rt-asr-model fun-asr-realtime \
  --rt-dingtalk-enabled
```

包含常用选项的示例：

```bash
python -m src.main analysis \
  --course-id <course_id> \
  --sub-id <sub_id> \
  --poll-interval 3 \
  --output-dir ./records \
  --rt-model gpt-4.1-mini \
  --rt-asr-scene zh \
  --rt-asr-model fun-asr-realtime \
  --rt-hotwords-file config/realtime_hotwords.json \
  --rt-keywords-file config/realtime_keywords.json \
  --rt-window-sentences 8 \
  --rt-stream-analysis-workers 32 \
  --rt-stream-queue-size 100 \
  --rt-asr-endpoint wss://dashscope.aliyuncs.com/api-ws/v1/inference \
  --rt-api-base-url https://aihubmix.com/v1 \
  --rt-alert-threshold 90 \
  --rt-dingtalk-enabled
```

启用 `--tingwu-enabled` 后，程序会额外录制整段音频，并在 `analysis` 退出后异步启动听悟后处理。

### 3. 按课表自动运行

复制示例配置：

```bash
cp config/auto_analysis.example.json config/auto_analysis.local.json
```

运行：

```bash
python -m src.main auto-analysis --config config/auto_analysis.local.json
```

一个最小配置大致如下：

```json
{
  "timezone": "Asia/Shanghai",
  "analysis_args": {
    "poll_interval": 3,
    "output_dir": "./records",
    "rt_model": "gpt-4.1-mini",
    "rt_asr_scene": "zh",
    "rt_asr_model": "fun-asr-realtime",
    "rt_hotwords_file": "config/realtime_hotwords.json",
    "rt_keywords_file": "config/realtime_keywords.json",
    "rt_dingtalk_enabled": true,
    "tingwu_enabled": false
  },
  "courses": [
    {
      "course_id": 90001,
      "title": "课程A",
      "teacher": "教师A",
      "slots": [
        {
          "start": "2099-03-09 08:00:00",
          "end": "2099-03-09 09:35:00"
        }
      ]
    }
  ]
}
```

注意：

- `courses[].course_id` 必填。
- `sub_id` 不需要写进课表，调度器会在课程时间附近探测直播状态并从接口返回中取得。
- 启动前会校验 `course_id`、课程标题和教师是否一致。
- `analysis_args` 里不要写 `course_id` / `sub_id`，调度器会在运行时注入。
- 示例里的日期请替换成未来课程时间；已经结束的 slot 会被当作历史课程跳过。
- 个人课表建议写在 `config/*.local.json`，这些文件已被 `.gitignore` 忽略。

## 独立麦克风链路

这条链路用于测试 ASR、关键词、系统提示词和钉钉提醒，不需要课程直播。

启动服务端：

```bash
SESSION_DIR="mic_session_$(date +%Y%m%d_%H%M%S)"

python -m src.main mic-listen \
  --host 127.0.0.1 \
  --port 18765 \
  --session-dir "$SESSION_DIR" \
  --mic-upload-token "change-this-token" \
  --rt-pipeline-mode stream \
  --rt-dingtalk-enabled \
  --rt-asr-scene zh \
  --rt-asr-model fun-asr-realtime \
  --rt-hotwords-file config/realtime_hotwords.json \
  --rt-keywords-file config/realtime_keywords.json \
  --rt-window-sentences 8 \
  --rt-stream-analysis-workers 32 \
  --rt-stream-queue-size 100 \
  --rt-model gpt-4.1-mini
```

如果服务端在远程机器上，可先转发端口：

```bash
ssh -N -L 18765:127.0.0.1:18765 <your-server>
```

在采集端列出麦克风设备并推送音频：

```bash
python -m src.main mic-list-devices

python -m src.main mic-publish \
  --target-url http://127.0.0.1:18765 \
  --mic-upload-token "change-this-token" \
  --device "你的麦克风设备名" \
  --rt-pipeline-mode stream \
  --stream-frame-duration-ms 120
```

## 自定义识别规则

实时分析主要依赖三个配置文件：

| 文件                                  | 作用                                 |
| ------------------------------------- | ------------------------------------ |
| `config/realtime_keywords.json`     | 事件分组、关键词、典型短语和细节线索 |
| `config/realtime_hotwords.json`     | 提供给 DashScope ASR 的热词          |
| `config/realtime_system_prompt.txt` | 约束 LLM 如何判断和输出              |

修改配置后需要重启 `analysis` 或 `mic-listen`。

校验 JSON：

```bash
python -m json.tool config/realtime_keywords.json > /dev/null
python -m json.tool config/realtime_hotwords.json > /dev/null
```

如果需要保留个人版本，可以新建：

```text
config/realtime_keywords.local.json
config/realtime_hotwords.local.json
config/auto_analysis.local.json
```

程序不会自动读取 `.local.json`；使用时需要在命令或课表配置中显式指定，例如 `--rt-keywords-file config/realtime_keywords.local.json`。

## 输出文件

### `analysis` 会话目录

| 文件                                      | 内容               |
| ----------------------------------------- | ------------------ |
| `realtime_transcripts.jsonl`            | 实时转写片段       |
| `realtime_insights.jsonl`               | 结构化分析结果     |
| `realtime_insights.log`                 | 中文可读分析日志   |
| `realtime_asr_events.jsonl`             | 流式 ASR 句级事件  |
| `analysis_prompt_trace.jsonl`           | LLM 请求与解析跟踪 |
| `realtime_dingtalk_trace.jsonl`         | 钉钉推送记录       |
| `realtime_runtime_heartbeat.jsonl`      | 运行态心跳         |
| `realtime_runtime_events.jsonl`         | 运行态事件         |
| `realtime_runtime_dingtalk_trace.jsonl` | 运行态告警推送记录 |

### `auto-analysis` 运行目录

| 文件                                            | 内容                            |
| ----------------------------------------------- | ------------------------------- |
| `auto_analysis_<timestamp>/auto_analysis.log` | 自动调度日志                    |
| `records/<course_session>/...`                | 每门课程各自的`analysis` 输出 |

### `mic-listen` 会话目录

| 文件                              | 内容                                          |
| --------------------------------- | --------------------------------------------- |
| `realtime_transcripts.jsonl`    | 麦克风实时转写片段                            |
| `realtime_insights.jsonl`       | 结构化分析结果                                |
| `realtime_insights.log`         | 中文可读分析日志                              |
| `realtime_asr_events.jsonl`     | stream 模式 ASR 事件                          |
| `analysis_prompt_trace.jsonl`   | LLM 请求与解析跟踪                            |
| `realtime_dingtalk_trace.jsonl` | 钉钉推送记录                                  |
| `realtime_profile.jsonl`        | 启用`--rt-profile-enabled` 后生成的性能日志 |

### 听悟后处理

| 文件                                   | 内容                           |
| -------------------------------------- | ------------------------------ |
| `tingwu_audio_full.mp3`              | 课程整段音频                   |
| `tingwu_audio_recording_report.json` | 录音分段与合并报告             |
| `tingwu_job.json`                    | 异步后处理任务描述             |
| `tingwu_process.log`                 | 听悟 worker 日志               |
| `tingwu_process_error.json`          | 失败详情                       |
| `tingwu_summary.md`                  | 听悟结果渲染后的 Markdown 汇总 |
| `tingwu_results/*.json`              | 听悟原始结果                   |

日志默认按大小轮转，避免长期运行时单个文件过大。

## 开发和测试

运行测试：

```bash
python -m unittest discover -s tests
```

测试集中在 CLI 参数解析、账号配置、课程扫描、直播状态判断、调度、日志轮转和听悟处理等基础行为。

完整测试和 `auto-analysis` 推荐在 Linux / WSL 运行；原生 Windows 只适合运行不导入自动调度模块的测试子集。

代码大致分布：

```text
src/auth/      登录和 token 管理
src/scan/      课程扫描与直播状态检查
src/live/      直播接入、实时分析、麦克风链路、自动调度
src/common/    配置、日志、HTTP、课程元数据等公共模块
tests/         单元测试
```

## 注意事项

- 实时识别和 LLM 判断都可能误报或漏报。
- 长期运行会产生音频、转写和日志文件，请定期清理 `records/` 与 `mic_session_*`。
- `.account`、本地课表、会话输出中可能包含账号、API Key、课程音频或通知内容，请妥善保存。
- 不要将本项目用于绕过课程要求、侵犯他人隐私或违反平台规则的用途。
