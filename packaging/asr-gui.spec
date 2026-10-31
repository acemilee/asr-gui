# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 asr_gui 打成一个单文件 exe（ASR-GUI.exe）。

构建（仓库根目录执行，需先装 pyinstaller）：
    python -m PyInstaller --clean --noconfirm packaging/asr-gui.spec

说明：
- 模型不打包。paraformer-zh 约 520 MB、whisper 各档 75 MB–3.1 GB，exe 只带程序本体，
  首次运行时由 GUI 的下拉菜单下载（或「定位/导入」指向已有缓存）。
- 不打包 CUDA 运行库（cublas/cudnn 约 1 GB）。因此 exe 里 faster-whisper 走 CPU；
  需要 GPU 的用户请用源码方式运行（见 README）。
- onefile 模式：运行时会解压到 %TEMP%\\_MEIxxxx。本程序的转写完全在进程内完成、
  不经过管道回传，所以不会重演上游那个 64 KiB 管道死锁（见 docs/lessons.md 第 1 节）。
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

HERE = Path(SPECPATH)  # noqa: F821  (SPECPATH 由 PyInstaller 注入)
REPO = HERE.parent

datas = []
binaries = []
hiddenimports = []

# 这些包带原生扩展或运行时数据，PyInstaller 的自动分析容易漏，显式全量收集。
for package in (
    "sherpa_onnx",  # paraformer-zh / VAD / 标点
    "sherpa_onnx_core",
    "onnxruntime",
    "av",  # PyAV：内置 FFmpeg
    "ctranslate2",  # faster-whisper 后端
    "faster_whisper",
    "tokenizers",
    "huggingface_hub",
):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception as exc:  # 没装的包直接跳过（exe 里对应档位会提示缺依赖）
        print(f"[spec] skip {package}: {exc}")
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# yt-dlp 是「视频链接」来源用的纯 Python 包，按子模块收集即可（不带元数据/可选依赖）。
try:
    hiddenimports += collect_submodules("yt_dlp")
except Exception as exc:
    print(f"[spec] skip yt_dlp: {exc}")

# 许可证随 exe 一起分发（MIT / 第三方声明）。
for extra in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
    candidate = REPO / extra
    if candidate.is_file():
        datas.append((str(candidate), "."))

a = Analysis(  # noqa: F821
    [str(REPO / "asr_gui" / "app.py")],
    pathex=[str(REPO / "asr_gui")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "pandas",
        "scipy",
        "torch",
        "PIL",
        "pytest",
        "ruff",
        "PyInstaller",
        "IPython",
        "pydoc_data",
        "tkinter.test",
        "unittest",
        "nvidia",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ASR-GUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # GUI 程序，不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(HERE / "icon.ico"),
    version=str(HERE / "version_info.txt"),
)
