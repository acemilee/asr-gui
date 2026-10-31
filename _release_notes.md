## asr-gui 0.1.0 — 本地长视频转文字（Windows 单文件版）

用本机算力把长音频/长视频转成带时间戳的中文文本。转写全程在进程内流式分块完成，不经过子进程管道，
因此不会出现上游打包版在 9 小时以上音频上卡死的现象（根因见 `docs/lessons.md` 第 1 节）。

### 下载

| 文件 | 说明 |
| --- | --- |
| `ASR-GUI-v0.1.0-win64.exe` | Windows 10/11 x64 单文件程序，约 109 MB，双击即用 |
| `SHA256SUMS.txt` | 校验值 |
| `Source code (zip)` / `Source code (tar.gz)` | 该 tag 的完整源码（GitHub 自动生成） |

校验下载（PowerShell / cmd）：

```bat
certutil -hashfile ASR-GUI-v0.1.0-win64.exe SHA256
```

### 第一次使用

1. 双击 `ASR-GUI-v0.1.0-win64.exe`；
2. 选模型：`paraformer-zh`（中文最快最准，仅 CPU，约 520 MB）或 `base`（体积小，约 142 MB）；
   未缓存的档位点「下载模型」，默认走 `hf-mirror.com` 镜像（可切官方源）；
   已经有模型的话，点「定位/导入…」直接指向缓存目录；
3. 选音频/视频文件 → 选输出目录 → 「开始转写」。

输出：`<名字>.txt`（人读，带 `[HH:MM:SS]` 时间戳）+ `<名字>.jsonl`（逐段增量落盘，支持断点续跑）。

### 命令行入口

```bat
ASR-GUI.exe --selftest "%TEMP%\asr-gui-selftest.txt"                     :: 环境自检，退出码 0 通过 / 2 有失败
ASR-GUI.exe --transcribe sample.m4a --out out.txt --model paraformer-zh   :: 无界面转写，日志写在 out.txt 旁边
```

### 说明

- 模型和 CUDA 运行库**不**随 exe 分发（体积原因）；exe 内 `faster-whisper` 走 CPU，需要 GPU 请从源码运行。
- 配置：`%APPDATA%\asr-gui\config.json`；模型缓存默认 `%APPDATA%\asr-gui\models`；
  崩溃日志：`%APPDATA%\asr-gui\logs\error-<日期>.log`。
- 实测（i9-13980HX 24 核 + RTX 4060 Laptop，CPU 16 线程）：10 小时音频用 `paraformer-zh` 约 **65 分钟**出稿。
- 已知限制：`paraformer-zh` 只能 CPU；不提供说话人分离；字节序/时间轴按 VAD 段落切分，
  跨块的段落可能有轻微重叠。

---

### English (short)

Single-file Windows build of asr-gui: offline long-audio speech-to-text with streamed chunked
decoding, resume support and an 8-model dropdown with mirror downloads. Models are **not** bundled —
download them from the GUI on first run (or point "Locate / import…" at an existing cache).
CLI: `--selftest` and `--transcribe`. Source-only users: see the README.
