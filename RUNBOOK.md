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

`find_course_id.py` 已废弃，仅用于提示迁移。

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
  --username <学号> \
  --password '<统一认证密码>' \
  --teacher '王强' \
  --title '测试标题' \
  --center 83650 \
  --radius 0 \
  --workers 1 \
  --retries 1 \
  --verbose
```

成功标志：

- 输出 JSON，`mode=scan`
- `matches` 中出现目标课程时即命中

### 4.2 直播服务（watch）
用途：持续拉取上游直播流并提供本地代理播放。

示例：

```bash
python -m src.main watch \
  --username <学号> \
  --password '<统一认证密码>' \
  --course-id 83650 \
  --sub-id 1895397 \
  --host 127.0.0.1 \
  --port 8765 \
  --poll-interval 3 \
  --playlist-retries 3 \
  --asset-retries 3 \
  --stale-playlist-grace 15 \
  --hls-max-buffer 20 \
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

### 4.3 本地浏览器访问（SSH 转发）
在本地机器执行：

```bash
ssh clusters -L 8765:127.0.0.1:8765
```

然后在本地浏览器打开：

- `http://127.0.0.1:8765/player?role=teacher`
- `http://127.0.0.1:8765/player?role=ppt`

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

## 6. 常见故障排查

### 6.1 `Login failed`
检查：

- 学号/密码是否正确
- 是否触发验证码（需要 `--authcode`）
- 集群网络是否可访问 CAS/API

### 6.2 `result_err_msg: 房间必传！`
原因：`sub_id` 不正确或为空。
处理：确认课程对应的真实 `sub_id` 后重试。

### 6.3 `/proxy/m3u8?role=teacher` 返回 `teacher stream not available`
原因：

- 当前轮询无教师流
- `sub_id` 指向的直播房间不对

处理：

- 先看 `/api/stream?role=teacher` 是否有 `stream`
- 看 `/api/streams` 的 `raw_streams` 是否有可用 m3u8
- 看 `/api/streams` 的 `active_provider`：`meta` 或 `livingroom`

### 6.4 浏览器无法播放 / 卡顿
检查：

- `hls.js` 是否成功加载（`/static/hls.min.js`）
- `/api/metrics` 是否出现连续失败上升
- 网络是否存在瞬时抖动（看 `asset_retry_successes`）
- `/api/stream?role=teacher` 中 `voice_track_on` 是否为 `false`（上游可能无音轨）
- `/api/streams` 的 `provider_diagnostics` 是否显示 `livingroom` 已产出 `stream_count>0`

说明：

- 当前播放器为“手动开始 + 自动恢复”模式：首次点击播放后自动维持连续播放，不提供额外声音开关。

优化建议：

- 提高 `--asset-retries`（如 5）
- 提高 `--playlist-retries`（如 5）
- 适当调大 `--hls-max-buffer`（如 30）

### 6.5 `Address already in use`
原因：端口被占用。
处理：

```bash
lsof -i :8765
# 或改用新端口
python -m src.main watch ... --port 18765
```

### 6.6 旧命令失效
`python find_course_id.py ...` 返回迁移提示属于预期，改用：

```bash
python -m src.main ...
```

## 7. 参数调优建议

低延迟优先（网络稳定）：

- `--poll-interval 3`
- `--hls-max-buffer 12~20`

稳定优先（网络波动）：

- `--playlist-retries 3~5`
- `--asset-retries 3~6`
- `--stale-playlist-grace 15~30`
- `--hls-max-buffer 20~40`

## 8. 运行安全与合规
- 不要在脚本、日志、截图中泄露账号密码。
- 优先通过受控环境变量或安全输入传递凭据（后续可演进）。
- 代理仅允许 `*.zju.edu.cn` / `*.cmc.zju.edu.cn`（代码已限制），不要放宽白名单。
- 仅在授权网络与账户权限范围内使用。

## 9. 发布前验收清单
- `python -m py_compile` 通过
- `python -m unittest discover -s tests -v` 全通过
- `scan` 命令可执行并输出 JSON
- `watch` 启动成功，可访问 `/api/metrics`
- `teacher/ppt` 页面均可打开
- 长时间运行后 `consecutive_*_failures` 能回落到 0

## 10. 快速命令速查

```bash
# 1) 扫描课程
python -m src.main scan --username <u> --password '<p>' --teacher '王强' --title '测试标题' --center 83650 --radius 0 --workers 1 --retries 1 --verbose

# 2) 启动直播代理
python -m src.main watch --username <u> --password '<p>' --course-id 83650 --sub-id <sub_id> --poll-interval 3 --port 8765 --no-browser

# 3) 查看指标
curl -s http://127.0.0.1:8765/api/metrics

# 4) 本地转发（在本地机器）
ssh clusters -L 8765:127.0.0.1:8765
```
