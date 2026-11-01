# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

---

## [0.1.3] - 2026-11-01

下载这一路更抗折腾：网络抖一下不再直接判死，失败时也告诉你该看哪儿。

Downloading became far more forgiving of flaky networks, and failures now say what to look at.

### Added / 新增

- **失败信息会翻译成人话**：命中 `WinError 10061/10054/10053`、`timed out`、`connection reset`、
  `Remote end closed` 时，错误里直接写明「网络侧问题（代理 / VPN / 加速器中途断开…）」，并提示
  「已下载的部分会续传，再点一次开始即可」，原始错误保留在末尾。`sources.describe_download_error()`。

### Changed / 变更

- **重试更耐用**：`retries` / `fragment_retries` 3 → **10**，新增 `extractor_retries`、
  `file_access_retries`，并用 `retry_sleep_functions` 做指数退避（1,2,4,8,16,20…秒，封顶 20）。
  之前一次 3 连败就整页失败，现在能扛住十几秒的网络中断。

### Fixed / 修复

- **点「停止」不再被 yt-dlp 拉起重试**：取消信号改为 `BaseException` 子类（`_CancelSignal`），
  穿透 yt-dlp 内部的 `except Exception`，下载立即中止（此前会把重试次数跑满，界面看起来像卡住）。
- **来源 / 分 P / 登录态不再每次启动被重置**：`config.defaults()` 缺这三个键，`load()` 会把
  已存的 `source` / `pages` / `cookies_from_browser` 静默丢掉（界面永远回到「本地文件」）。
  现在三项都进了 defaults，并有回归测试锁住。

## [0.1.2] - 2026-11-01

视频链接这一路补齐了 B 站的三件事：**AI 字幕优先**（能拿到字幕就秒级出稿、完全不占模型）、
**浏览器登录态**（拿 AI 字幕通常要登录）、**分 P 选择**（`1,3-5`）。上游这三项都做在 B 站/抖音专用
代码里，这里做成了通用编排层。

Bilibili links get the three things that actually matter: AI subtitles first (seconds instead of hours),
browser login state (the AI subtitle track usually needs it), and per-page selection (`1,3-5`).

### Added / 新增

- **字幕优先（秒级出稿）**：B 站链接默认先查 AI 字幕轨（`x/player/wbi/v2`，带 wbi 签名）；
  有字幕就直接转成 txt + jsonl，**不下载音频、不加载模型**。没有字幕（未登录 / 该 P 无字幕轨）
  自动回落语音转写，日志里会写明原因。
- **分 P 支持**：`分 P` 输入框（只在「视频链接」来源下出现）与命令行 `--pages 1,3-5`；
  留空 = 全部。多分 P 时每 P 单独下载（`?p=N`），时间轴按前序 P 的时长累加，最后合并成一份 txt。
- **登录态**：`登录态` 下拉（不使用 / Chrome / Edge / Firefox）从本机浏览器读 B 站 cookie，
  命令行对应 `--cookies-from-browser chrome` 或 `--cookies cookies.txt`（Netscape 格式）。
  读不到只记一行日志，不阻断流程。
- **`bili.py`**：纯标准库的 B 站接口层（`fetch_video` / `find_subtitle` / `fetch_subtitle_segments` /
  `parse_page_spec` / `with_page` / `select_pages`），不依赖第三方 SDK。
- **`pipeline.py`**：GUI 与命令行共用的编排层（`run_url_pipeline`），单文件链接与分 P 视频走同一套逻辑。
- **命令行**：`--pages`、`--cookies-from-browser`、`--cookies`、`--no-subtitle-first`。

### Changed / 变更

- 链接模式的输出命名：多分 P 时先写 `<名字>.p1.jsonl`、`<名字>.p2.jsonl`，结束后按时间轴合并成
  `<名字>.txt`；单文件链接仍是 `<名字>.jsonl`。
- `config.py` 新增 `pages` 与 `cookies_from_browser` 两个配置项；`APP_VERSION` 升到 `0.1.2`。
- 「进度」在链接模式下会同时反映下载字节进度（`下载中 12.3 MB / 50.0 MB`）与已处理时长。

### Fixed / 修复

- `_run_bilibili` 的音频下载没有透传 cookie 参数（登录态下载会 403）。
- `bili._pages_from` 只接受 `{"pages": [...]}`，而 pagelist 接口的 `data` 本身就是列表，
  风控回退路径会 `AttributeError`。
- `load_cookie_file` 遇到不存在的文件会抛异常，改为返回空字典。
- **后台线程读写 Tk 变量**（`_url_worker` 里读 `var_pages` / `var_cookies`）会在非主线程抛
  `RuntimeError: main thread is not in main loop`；改为在 `on_start`（主线程）取值后作为参数传入。

### Tests / 测试

- 新增 `tests/test_bili.py`（45 个用例）、`tests/test_pipeline.py`（18 个用例），
  `tests/test_sources.py` 补 cookie 相关 9 个，`tests/test_app_contract.py` 补 6 个；
  **共 221 个用例**，`ruff` 全绿。

## [0.1.1] - 2026-10-31

加上「输入来源」：不再只能喂本地文件，可以直接贴视频链接，或者吃现成字幕。

Adds input sources: paste a video URL, or feed an existing subtitle file, instead of only local files.

### Added / 新增

- **三种输入来源下拉**：「视频链接 / 本地文件 / 字幕文件」。
  - 视频链接：用 yt-dlp 只下载音频轨（不转码、不下画面），落到 `<程序目录>/downloads`
    （exe 版为 `%APPDATA%\asr-gui\downloads`），再走原有的分块转写；默认只取第一个分 P。
  - 字幕文件：直接解析 `.srt` / `.vtt`（含 BOM、GB18030、VTT 标签与 `NOTE` 块），
    不加载模型，瞬间写 txt + jsonl。
  - 「浏览…」按钮会随来源变形：链接模式变成「粘贴」，直接读剪贴板。
- **下载进度显示**：下载阶段报字节进度（`下载中 12.3 MB / 50.0 MB`），与转写的时间轴进度分开显示。
- **`--selftest` 覆盖 yt-dlp**：自检报告里会列出 yt-dlp 是否可用。
- **`--transcribe` 也支持三种来源**：直接传视频页链接（自动下音频再转写）或 `.srt` / `.vtt`
  字幕文件（不加载模型），批处理/服务器场景不必再开界面。

### Changed / 变更

- `config.py` 新增 `downloads_dir()`；`APP_VERSION` 升到 `0.1.1`（同步 `pyproject.toml` 与 exe 版本资源）。
- `packaging/asr-gui.spec` 用 `collect_submodules("yt_dlp")` 收集 yt-dlp（纯 Python 包，
  比 `collect_all` 干净），exe 内自带 yt-dlp。
- **模型缓存目录统一走 `config.effective_cache_dir()`**：优先级 `ASR_GUI_MODEL_DIR` 环境变量
  > 配置文件 > 自动发现；修掉「界面显示已缓存、命令行却找不到模型」的不一致。
- **`packaging/build.bat` 加解释器预检（`[0/3]`）**：缺 `numpy/av/sherpa_onnx/ctranslate2/
  faster_whisper/tokenizers/yt_dlp/PyInstaller` 时直接报错退出，并提示用 `ASR_PYTHON` 指定
  正确的解释器。此前它会静默挑 PATH 上的 python，导致打进别人家的 tensorflow 依赖树、
  产物从 109 MB 涨到 588 MB 且运行时缺依赖（见 `docs/lessons.md` 第 9 节）。
- 依赖新增 `yt-dlp>=2024.8.6`。

### Tests / 测试

- 新增 `tests/test_sources.py`（38 个用例：时间戳、标签清洗、SRT/VTT 解析、编码回退、
  URL 判定、下载参数、无 yt-dlp 时的错误路径、JSONL 格式），总计 139 个用例。

## [0.1.0] - 2026-10-31

第一个公开发布版，面向「本地长视频转文字」这个场景：不再把整段音频读进内存，也不再经过进程间管道，
因此不会重演上游打包版在 9 小时以上音频上卡死的问题（见 `docs/lessons.md` 第 1 节）。

First public release. Built for long recordings: audio is streamed in chunks and never pushed through
a subprocess pipe, so the upstream deadlock on 9-hour inputs does not apply here
(see `docs/lessons.md`, section 1).

### Added / 新增

- **8 档模型下拉**：`paraformer-zh`（中文最快最准，仅 CPU）、`tiny` / `base` / `small` / `medium` /
  `large-v3` / `turbo` / `belle-turbo-zh`，每档实时显示「已缓存 / 未缓存 · 需下载约 N MB」。
- **镜像下载**：默认 `hf-mirror.com`，可切官方 `huggingface.co`；支持断点续传（`.part`）、
  下载进度、取消、完整性与体积校验（不完整文件绝不改成正式文件）。
- **手动导入**：`定位/导入…` 可直接指向已有的 `models--Org--Name/snapshots/...` 或 sherpa 模型目录，
  通过校验后写入配置。
- **断点续跑**：`<名字>.jsonl` 逐段增量落盘，中断后可从上次位置继续；`<名字>.txt` 同步可读。
- **进度反馈**：进度条 + 百分比 + 已处理时长 + 实时倍速 + ETA，转写中界面不阻塞，停止按钮可随时优雅中断。
- **单文件 exe**：`packaging/` 下的 PyInstaller 配置可打出 `ASR-GUI.exe`（约 109 MB），
  附带图标、版本资源与 `--selftest` / `--transcribe` 两个命令行入口。
- **配置外置**：配置在 `%APPDATA%\asr-gui\config.json`，崩溃日志写 `%APPDATA%\asr-gui\logs\`。
- **测试与 CI**：`tests/` 94 个用例（引擎分块/进度、模型注册表、下载器、配置、GUI 结构契约），
  `ruff` 静态检查，GitHub Actions 在 Windows + Ubuntu、Python 3.11/3.13 上全绿，
  另有专门的 `.bat` 编码回归任务。

### Notes / 说明

- 模型与 CUDA 运行库不随 exe 分发：`paraformer-zh` 约 520 MB，whisper 各档 75 MB–3.1 GB。
- exe 内 `faster-whisper` 走 CPU；要用 GPU 请从源码运行（`pip install -r requirements.txt` 后
  `python asr_gui/app.py`，`nvidia-*` 依赖会自动带上 CUDA 12 运行库）。
- 实测（i9-13980HX + RTX 4060 Laptop）：10 小时音频用 `paraformer-zh`（CPU，16 线程）约 65 分钟出稿；
  30 分钟短音频的短切片基准外推约 15–20 分钟，长片会因持续满载降频变慢。
- 转写完成后不要把长稿丢回上游 App 的「LLM 笔记」阶段：那里对超长文本没有任务级超时。

[0.1.0]: https://github.com/acemilee/asr-gui/releases/tag/v0.1.0
