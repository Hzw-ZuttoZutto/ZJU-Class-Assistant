# ZJU Classroom Live Tool

直播代理与课程扫描工具，统一入口为：

```bash
python -m src.main <subcommand> ...
```

## 目录

- `src/main.py`：CLI 总入口
- `src/scan/`：课程扫描逻辑
- `src/live/`：直播轮询、代理、Web 服务
- `src/live_video.py`：教师流选择策略（优先音轨可用）
- `src/live_ppt.py`：PPT 流选择策略
- `tests/`：单元与集成测试
- `RUNBOOK.md`：运行与排障手册

## 快速开始

```bash
# 凭据文件（推荐，避免在命令行暴露密码）
cat > .account <<'EOF'
USERNAME=你的统一认证账号
PASSWORD=你的统一认证密码
EOF

# 语法检查 + 测试
python -m py_compile $(find src tests -name '*.py')
python -m unittest discover -s tests -v

# 扫描课程
python -m src.main scan --teacher '王强' --title '测试标题' --center 83650 --radius 0 --workers 1 --retries 1 --verbose

# 扫描课程（仅保留“直播中”）
python -m src.main scan \
  --teacher '王强' \
  --title '测试标题' \
  --center 83650 \
  --radius 0 \
  --require-live \
  --live-check-timeout 30 \
  --live-check-interval 2

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
```

凭据规则：

- 默认从工作区根目录 `.account` 读取 `USERNAME` 和 `PASSWORD`。
- 仍可通过 `--username/--password` 显式传入；CLI 传参优先级更高。

录制产物：

- 每段 `mp4`：`课程名_老师名_开始时间_结束时间.mp4`
- 每段 `mp3`：`课程名_老师名_开始时间_结束时间.mp3`
- 每段缺失日志：`课程名_老师名_开始时间_结束时间.missing.json`
- 会话汇总：`recording_session_report.json`

本地浏览器观看（本地执行）：

```bash
ssh clusters -L 8765:127.0.0.1:8765
```

然后打开：

- `http://127.0.0.1:8765/player?role=teacher`
- `http://127.0.0.1:8765/player?role=ppt`
