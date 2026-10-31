# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

---

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
