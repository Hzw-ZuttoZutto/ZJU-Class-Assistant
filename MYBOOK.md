  # MYBOOK: 独立麦克风上游无阻碍运行手册

目标：稳定跑通 `mic-listen + ssh -L + mic-publish`，并能快速确认链路是否打通。

## 0. 固定参数
- 集群项目目录：`/home/placitudo/fuckclass`
- 集群 Python：`/home/placitudo/APP/miniconda3/envs/fuckclass/bin/python`
- 本机 Python：`D:\All_The_App\anaconda3\envs\fuckclass\python.exe`
- 本机 ffmpeg：`D:\All_The_App\Anaconda3\envs\fuckclass\Library\bin\ffmpeg.exe`
- 上行端口：`18765`
- 上传 token：`YOUR_TOKEN`

## 1. 集群端（终端 A）
先登录：

```bash
ssh clusters
```

然后执行：

```bash
cd /home/placitudo/fuckclass
/home/placitudo/APP/miniconda3/envs/fuckclass/bin/python -m src.main mic-listen \
  --host 127.0.0.1 \
  --port 18765 \
  --mic-upload-token YOUR_TOKEN \
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

不要关闭这个终端。

## 2. 本机端口转发（终端 B, PowerShell）

```powershell
chcp 65001 > $null
$env:PYTHONIOENCODING='utf-8'
ssh -N -L 18765:127.0.0.1:18765 clusters
```

不要关闭这个终端。

## 3. 本机发布麦克风（终端 C, PowerShell）
先列设备（确认设备名必须包含 `®`）：

```powershell
$py='D:\All_The_App\anaconda3\envs\fuckclass\python.exe'
$ff='D:\All_The_App\Anaconda3\envs\fuckclass\Library\bin\ffmpeg.exe'

chcp 65001 > $null
$env:PYTHONIOENCODING='utf-8'

& $py -m src.main mic-list-devices --ffmpeg-bin $ff
```

再启动发布：

```powershell
$py='D:\All_The_App\anaconda3\envs\fuckclass\python.exe'
$ff='D:\All_The_App\Anaconda3\envs\fuckclass\Library\bin\ffmpeg.exe'

chcp 65001 > $null
$env:PYTHONIOENCODING='utf-8'

& $py -m src.main mic-publish --target-url http://127.0.0.1:18765 --mic-upload-token YOUR_TOKEN --device "麦克风阵列 (适用于数字麦克风的英特尔® 智音技术)" --chunk-seconds 10 --ffmpeg-bin $ff --work-dir .mic_publish_chunks_run_01
```

## 4. 链路验证（终端 D, PowerShell）

```powershell
$py='D:\All_The_App\anaconda3\envs\fuckclass\python.exe'
& $py -c "import requests;print(requests.get('http://127.0.0.1:18765/api/mic/metrics',timeout=5).json())"
```

判定标准：
- `uploaded_total` 持续增长
- `accepted_total` 跟随增长
- `processed_total` 跟随增长
- `auth_failures=0`
- `process_failures` 不持续增长

## 5. 常见卡点（你这次踩到的）
- 本机和集群没用同一个 `fuckclass` 环境。
- 本机 `ffmpeg` 不在 PATH，必须加 `--ffmpeg-bin`。
- 设备名写成 `?` 而不是 `®`，`ffmpeg` 会秒退。
- 未设置 UTF-8 控制台，打印设备名可能触发 `UnicodeEncodeError`。
- 集群没设置 `OPENAI_API_KEY`。

## 6. 停止与清理
- 终端 C 按 `Ctrl+C`（停 `mic-publish`）
- 终端 B 按 `Ctrl+C`（停 SSH 转发）
- 终端 A 按 `Ctrl+C`（停 `mic-listen`）

可选清理本地切片：

```powershell
Remove-Item -Recurse -Force .mic_publish_chunks_run_01
```
