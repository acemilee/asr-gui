# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

---

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
