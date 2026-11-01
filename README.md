# asr-gui

**Turn hours-long videos into timestamped transcripts, entirely offline.** A small tkinter
desktop app on top of [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) and
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) — no cloud APIs, no uploads, no
Electron/Qt runtime, just Python 3.9+ and the standard library.

[![CI](https://github.com/acemilee/asr-gui/actions/workflows/ci.yml/badge.svg)](https://github.com/acemilee/asr-gui/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)
[![Release](https://img.shields.io/github/v/release/acemilee/asr-gui)](https://github.com/acemilee/asr-gui/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/acemilee/asr-gui/total)](https://github.com/acemilee/asr-gui/releases)

![GUI](asr_gui/GUI界面.png)

![Video URL source](asr_gui/GUI视频链接.png)

![Model dropdown](asr_gui/GUI模型下拉.png)

## Why this exists

Most "video to notes" tools are fine until the video gets long. Packaged builds that stream the
decoded audio to a child process over a pipe silently stall on multi-hour files once the pipe
buffer fills — a 9-hour video that "hangs forever" at 80% is the classic symptom. `asr-gui` is a
deliberate single-process rewrite: audio is decoded in bounded chunks, results are checkpointed to
disk after every chunk, and the whole run can be cancelled and resumed at any time.

## Features

- **Three input sources** - paste a video page URL (Bilibili / Douyin / YouTube) and only the
  audio track is downloaded (no transcoding, no video bytes), pick a local audio/video file, or feed
  a `.srt` / `.vtt` subtitle file straight through - the subtitle path skips the models entirely and
  finishes in a second. URL downloads use [yt-dlp](https://github.com/yt-dlp/yt-dlp).
- **Bilibili niceties** - AI subtitles first (instant output, no model, no download); per-page
  selection (`1,3-5`, blank = all, timelines offset and merged into one text); browser login state
  (Chrome / Edge / Firefox) because the AI subtitle track usually needs a logged-in session.
  No login - automatic fallback to speech recognition, with the reason in the log.
- **8 model tiers in one dropdown** — from `tiny` (≈75 MB, instant) to `large-v3` (≈3.1 GB) and
  the Chinese-tuned `paraformer-zh`. The list shows `已缓存` (cached) or
  `未缓存·需下载 ~XXX MB` (not cached · download size) per tier.
- **Mirror-first model downloads** — defaults to `https://hf-mirror.com` and can be switched to
  `https://huggingface.co` in the UI. Downloads are resumable (`*.part`), integrity-checked against
  `Content-Length`, and retried; no `huggingface_hub` snapshot machinery is involved.
- **Resume from where it stopped** — every recognised segment is appended to a JSONL checkpoint
  file. Reopen the same output folder and the app offers to continue instead of starting over.
- **Cancellable** — Stop halts the worker within one chunk and keeps everything written so far.
- **Long audio is the design target** — a 10-hour / 35,990 s video completes in about 65 minutes on
  CPU with `paraformer-zh` (measured, see [Performance](#performance)).
- **tkinter only** — GUI in the standard library; runtime deps are the ASR stack and nothing else.
- **Offline after setup** — once the model is cached the network is not touched again.
- **Readable + machine-readable output** — a `[HH:MM:SS] text` file for humans and a JSONL file
  for tooling, both written incrementally.
- **Bring your own model folder** — *Locate / import…* points at an already-downloaded model
  directory (for example a cache left behind by another tool). The folder is validated file by
  file, so a half-downloaded copy is rejected instead of failing at load time.
- **Config and logs live outside the repo** — UI choices are saved to the per-user config
  directory, so nothing machine-specific is hard-coded in the source. Unhandled exceptions are
  appended to `logs/error-<date>.log` rather than vanishing with a traceback dialog.

## Quick start

Runtime dependencies (`pip install -r requirements.txt`): `sherpa-onnx` (paraformer-zh / VAD /
punctuation), `faster-whisper` + `ctranslate2` (whisper tiers), `av` (PyAV, streaming decode),
`numpy`, and `yt-dlp` (only for the video-URL source).

Requirements: **Python 3.9+**, a CPU that can run ONNX/CTranslate2 (any x86-64 or Apple Silicon
laptop is fine), and optionally an NVIDIA GPU with CUDA 12 for the faster-whisper path.

### Windows without Python: grab the exe

Download `ASR-GUI-v0.1.0-win64.exe` (~109 MB) from the
[Releases](https://github.com/acemilee/asr-gui/releases) page and double-click it. Models are not
bundled: pick one in the dropdown and press **Download model**, or point **Locate / import…** at an
existing cache directory. Verify the download with:

```bat
certutil -hashfile ASR-GUI-v0.1.0-win64.exe SHA256
```

The exe also accepts two command-line entry points (handy for troubleshooting and batch jobs):

```bat
ASR-GUI.exe --selftest "%TEMP%\asr-gui-selftest.txt"                     :: environment check
ASR-GUI.exe --transcribe sample.m4a --out out.txt --model paraformer-zh   :: local file
ASR-GUI.exe --transcribe "https://www.bilibili.com/video/BV..." --out out.txt  :: url (audio only)
ASR-GUI.exe --transcribe sample.srt --out out.txt                         :: subtitle file
```

`--transcribe` accepts the same three sources as the GUI: a local media file, a video page
URL (audio only), or a `.srt` / `.vtt` subtitle file (no model needed).

### Windows from source

```bat
setup.bat
asr_gui\run.bat
```

Or double-click `asr_gui\启动转写GUI.bat`. `run.bat` finds an interpreter in this order:
`ASR_PYTHON` → `asr_gui\config.local.bat` → repo-root `.venv` → `python.exe` on `PATH` → a portable
venv, pre-checks `import sherpa_onnx, av, numpy`, and then prefers `pythonw.exe` so no console
window stays open.

If you already have a Python with the dependencies installed:

```bat
python asr_gui\app.py
```

### Linux / macOS

```bash
./setup.sh
.venv/bin/python asr_gui/app.py
```

The GUI needs tkinter, which some distributions ship separately:
`sudo apt install python3-tk` (Debian/Ubuntu), `sudo dnf install python3-tkinter` (Fedora),
`brew install python-tk` (Homebrew). PyAV bundles its own FFmpeg, so no `ffmpeg` install is needed.

### GPU (optional)

faster-whisper uses CTranslate2, which needs the CUDA 12 cuBLAS and cuDNN runtime:

```bash
python -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

The `gpu` extra in `pyproject.toml` lists the same two packages (enabled once the packaging story is
settled). `paraformer-zh` always runs on CPU; the UI locks the device selector to CPU when it is
selected.

## Input sources

The "source" dropdown at the top decides what one run consumes:

| Source | You provide | What happens | Needs a model |
| --- | --- | --- | --- |
| Video URL | a page URL (`https://...`); the 粘贴 button pastes from the clipboard, and Bilibili's "copy share" blob (title + link) works too — only the link is kept | yt-dlp downloads the audio track only into `<app dir>/downloads` (`%APPDATA%\asr-gui\downloads` for the exe), then the usual chunked transcription runs | yes |
| Local file | path to an audio or video file | decoded and transcribed directly; common containers are fine | yes |
| Subtitle file | a `.srt` / `.vtt` file | parsed into txt + jsonl immediately | **no** |

Downloading audio only is deliberate: a video track can be gigabytes while transcription needs just
the audio, and PyAV already decodes `m4a` / `webm` without an ffmpeg binary. Pressing stop mid-download
aborts yt-dlp and leaves the `.part` fragments in `downloads` for the next attempt to resume.

## Models

All tiers are downloaded on demand from Hugging Face (or the mirror) and cached locally. The repo
names below are the CTranslate2 / sherpa-onnx conversions that are actually used.

| Tier | Backend | Hugging Face repo | Download | Device |
| --- | --- | --- | --- | --- |
| `tiny` | faster-whisper (CT2) | `Systran/faster-whisper-tiny` | ~75 MB | CPU / CUDA |
| `base` | faster-whisper (CT2) | `Systran/faster-whisper-base` | ~142 MB | CPU / CUDA |
| `small` | faster-whisper (CT2) | `Systran/faster-whisper-small` | ~466 MB | CPU / CUDA |
| `medium` | faster-whisper (CT2) | `Systran/faster-whisper-medium` | ~1.5 GB | CPU / CUDA |
| `large-v3` | faster-whisper (CT2) | `Systran/faster-whisper-large-v3` | ~3.1 GB | CPU / CUDA |
| `turbo` | faster-whisper (CT2) | `deepdml/faster-whisper-large-v3-turbo-ct2` | ~1.6 GB | CPU / CUDA |
| `belle-turbo-zh` | faster-whisper (CT2) | `wolfofbackstreet/faster-whisper-belle-whisper-large-v3-turbo-zh-ct2-int8` | ~0.8 GB | CPU / CUDA |
| `paraformer-zh` | sherpa-onnx | `csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14` + punctuation + VAD repos | ~520 MB total | CPU only |

Notes:

- `paraformer-zh` is the default and the best choice for Chinese: non-autoregressive (fast on CPU),
  accurate, and punctuation is added by a separate `ct-transformer` model. It is a bundle of three
  repos — ASR (`model.int8.onnx` + `tokens.txt`), punctuation (`model.onnx`) and Silero VAD.
- The whisper CT2 conversions differ in which files they ship (some use `vocabulary.txt`, others
  `vocabulary.json` plus a required `preprocessor_config.json`); the downloader requests the right
  set per tier and validates file sizes so a truncated cache can never be mistaken for a good one.
- Model weights keep whatever license their upstream repos declare — see
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

### How downloads work

The downloader issues a plain streaming `GET {endpoint}/{repo}/resolve/main/{file}` and writes to
`<file>.part`, resuming with a `Range` header and verifying `Content-Length` before renaming into
place. `huggingface_hub.snapshot_download` is intentionally **not** used: the mirror's 302
redirects omit the `X-Repo-Commit` header, which newer `hf_hub` versions reject as a failed
integrity check.

## Output

Both files land in the chosen output directory:

`<name>.txt` — human-readable:

```text
[00:00:03] 大家好，欢迎来到本期节目。
[00:00:09] 今天我们聊一聊长视频转写。
```

`<name>.jsonl` — one JSON object per segment, used for resume and for downstream tooling:

```json
{"start": 3.52, "end": 8.97, "text": "大家好，欢迎来到本期节目。"}
{"start": 9.31, "end": 13.04, "text": "今天我们聊一聊长视频转写。"}
```

## Performance

Measured on Windows, i9-13980HX (24C/32T), 63.6 GB RAM, RTX 4060 Laptop 8 GB, on a real 10-hour
Chinese video (35,990 s of audio):

| Run | Hardware | Time | RTF |
| --- | --- | --- | --- |
| `paraformer-zh`, 16 CPU threads, full 10 h file | CPU | **64.8 min** | 0.108 |
| `faster-whisper base`, fp16, short slices | RTX 4060 Laptop | — | 0.031 |
| `large-v3-turbo`, fp16, short slices | RTX 4060 Laptop | — | 0.035 |

**Rule of thumb: allow 60–70 minutes per 10 hours of audio** for `paraformer-zh` on a desktop CPU.
The whisper numbers are short-slice benchmarks; extrapolating them to a full file underestimates the
real cost, because sustained full-load thermal throttling on a laptop slows throughput by roughly
3–4× (early 300 s chunks took ~10 s, late ones ~32 s). The whisper full-file runs have not been
measured end to end and should be expected on the order of 20–30 minutes for 10 hours on the quoted
GPU.

Decoding is cheap by comparison: PyAV → 16 kHz mono runs at RTF ≈ 0.0014, and seeking to 18,000 s
takes 0.4 s.

## How it works

```text
video/audio file
   │  PyAV streaming decode, 300 s per chunk
   ▼
16 kHz mono float32
   │  sherpa-onnx Silero VAD → speech segments
   ▼
paraformer-zh (CPU)  or  faster-whisper (CPU/CUDA)
   │  ct-transformer punctuation (paraformer path)
   ▼
incremental append → <name>.jsonl + <name>.txt
```

| File | Role |
| --- | --- |
| `asr_gui/engine.py` | Transcription engine: chunked decode, VAD, recognition, punctuation, incremental JSONL, resume, cancel |
| `asr_gui/app.py` | tkinter GUI (single entry point) |
| `asr_gui/models_registry.py` | The 8 tiers, cache detection, file-name aliases, integrity checks |
| `asr_gui/downloader.py` | Streaming downloads from mirror/official endpoints, `.part` resume, size checks |
| `asr_gui/config.py` | User config read/write, cache-directory discovery, default output folder |
| `asr_gui/run.bat` | ASCII-only launcher: interpreter discovery + dependency pre-check |
| `asr_gui/启动转写GUI.bat` | Four-line wrapper around `run.bat`, kept for the double-click habit |
| `asr_gui/config.local.bat` | Machine-local interpreter override (git-ignored) |

## Configuration and cache locations

| Environment variable | Purpose |
| --- | --- |
| `ASR_PYTHON` | Interpreter used by `run.bat` (takes precedence) |
| `ASR_GUI_MODEL_DIR` | Model cache root; pointing it at an existing cache makes those tiers show as cached |
| `ASR_GUI_CONFIG_DIR` | Overrides the config directory (default `%APPDATA%\asr-gui`) |

UI choices (model, device, threads, download endpoint, output folder, last audio file) are written
to `config.json` when a run starts and when the window closes. Models default to
`%APPDATA%\asr-gui\models`; change `model_cache_dir` (or the env var above) to keep them on
another drive.

## Development and tests

```bash
python -m pip install -r requirements-dev.txt   # numpy + av + pytest + ruff only
python -m pytest tests -q                       # 90 tests, ~3 s, no network, no model downloads
python -m ruff check asr_gui tests
```

The suite deliberately avoids the inference runtimes (`sherpa_onnx` / `faster_whisper` are imported
lazily inside the engine), so CI never has to pull gigabytes of model runtime. Cache-related cases
fake the `models--Org--Name/snapshots/*` layout with temp dirs and sparse files:

| File | Coverage |
| --- | --- |
| `tests/test_engine_utils.py` | timestamp formatting, duration probing, 300 s chunk boundaries, resume-point parsing |
| `tests/test_models_registry.py` | the 8 tiers, complete/incomplete caches, older-snapshot fallback, size floors, manual-folder validation |
| `tests/test_downloader.py` | candidate URL order, cancel semantics, `.part` handling, "no `huggingface_hub`" regression |
| `tests/test_config.py` | config round-trip, unknown keys dropped, corrupt JSON fallback, cache-dir discovery order |
| `tests/test_app_contract.py` | `app.py` syntax/import/contract, no hard-coded machine paths, `.bat` must stay ASCII+CRLF |

`.github/workflows/ci.yml` runs three things: `ruff` + `compileall`; `pytest` on Windows/Ubuntu ×
Python 3.11/3.13; and a dedicated `.bat` encoding check (regression for the lesson #7 trap).

## Limitations

Known and accepted for v0.1:

1. **Progress granularity is one chunk.** The progress bar advances once per 300 s block; within a
   block there is no finer feedback.
2. **Cancelling may skip the final progress callback**, so the bar stops at the last reported
   percentage even though everything up to that point is safely written.
3. **Resume is approximate.** The resume point is the maximum `end` in the JSONL, so a VAD segment
   that straddles a chunk boundary can be emitted twice or lose a small piece when you continue a
   run.
4. **No percentage when duration is unknown.** If PyAV cannot report the container duration, the
   progress bar falls back to indeterminate-style updates with RTA/ETA only.
5. **Cancelling whisper runs takes effect at chunk boundaries only.** The paraformer path checks the
   cancel flag every ~32 ms and feels immediate; faster-whisper cannot be interrupted mid-segment.
6. **The GUI is Chinese-only** at the moment; the CLI output and this README are language-neutral.
7. **Single file per run.** Batch queues and subtitle export are not implemented yet.

## Acknowledgements

- Inspired by [like-attract/video-to-note](https://github.com/like-attract/video-to-note) (MIT).
  That project's packaged build is where the 64 KiB subprocess pipe deadlock on very long videos was
  diagnosed; this repository's engine is an independent rewrite (single process, no cross-process
  pipes, chunked checkpoints, cancellable). The model registry / repo mappings were cross-checked
  against the upstream implementation, which is acknowledged in
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
  [CTranslate2](https://github.com/OpenNMT/CTranslate2) and [PyAV](https://github.com/PyAV-Org/PyAV)
  do the actual heavy lifting.

## License

[MIT](LICENSE) © 2026 asr-gui contributors. Third-party components and model weights are covered by
their own licenses — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

中文文档：[README.zh-CN.md](README.zh-CN.md)
