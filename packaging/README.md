# 打包说明 / Packaging

把 `asr_gui/` 打成单文件 Windows exe（`ASR-GUI.exe`，约 109 MB）。

Building a single-file Windows exe from `asr_gui/` (~109 MB).

## 需要什么 / Requirements

- Windows 10/11 x64
- Python 3.9+（发布版用 3.13 构建 / the release was built with 3.13）
- `python -m pip install -r requirements.txt -r requirements-dev.txt`（含 PyInstaller）

## 怎么构建 / How to build

```bat
packaging\build.bat
```

或手动 / or manually:

```bat
python packaging\make_icon.py          :: 首次生成 icon.ico（Pillow）
python -m PyInstaller --clean --noconfirm packaging\asr-gui.spec
```

产物 / output：`dist\ASR-GUI.exe`。

## 文件 / Files

| 文件 | 作用 |
| --- | --- |
| `asr-gui.spec` | PyInstaller 配置：onefile、无控制台、图标、版本资源、原生依赖全量收集 |
| `version_info.txt` | exe 文件属性里的版本与版权（与 `asr_gui/config.py` 的 `APP_VERSION` 同步） |
| `make_icon.py` | 用 Pillow 生成 `icon.ico`（深色圆角 + 波形条） |
| `build.bat` | 一键构建（纯 ASCII + CRLF，符合仓库的 .bat 规范） |

## 打进去什么 / What goes in

- 程序本体、`engine.py` / `config.py` / `downloader.py` / `models_registry.py`
- tkinter、PyAV（自带 FFmpeg）、sherpa-onnx（paraformer-zh + VAD + 标点）、
  faster-whisper / ctranslate2 / tokenizers / huggingface_hub
- `LICENSE`、`THIRD_PARTY_NOTICES.md`

## 故意不打包什么 / What is deliberately excluded

| 不打包 | 原因 |
| --- | --- |
| 模型文件 | paraformer-zh 约 520 MB，whisper 各档 75 MB–3.1 GB；首次运行由界面下载，或「定位/导入」指向已有缓存 |
| CUDA 运行库（cublas/cudnn，约 1 GB） | exe 里 faster-whisper 走 CPU；要 GPU 请用源码方式运行 |
| PyTorch / scipy / matplotlib 等 | 用不到，只会让包变大 |

## 打包后的路径约定 / Runtime paths when frozen

| 项目 | 位置 |
| --- | --- |
| 配置 | `%APPDATA%\asr-gui\config.json` |
| 模型缓存（默认） | `%APPDATA%\asr-gui\models` |
| 崩溃日志 | `%APPDATA%\asr-gui\logs\error-<日期>.log` |
| 默认输出目录 | `%USERPROFILE%\asr-gui-output` |
| onefile 解包 | `%TEMP%\_MEIxxxx`（退出后自动清理） |

## 构建后自检 / Post-build check

```bat
dist\ASR-GUI.exe --selftest "%TEMP%\asr-gui-selftest.txt"
```

退出码 `0` = 全部通过，`2` = 有项目失败（报告文件里会写明是哪一项）。
想验证真正转写，可用命令行模式：

```bat
dist\ASR-GUI.exe --transcribe sample.m4a --out out.txt --model paraformer-zh
```

（`.log` 会写在 `out.txt` 旁边，方便排查。）
