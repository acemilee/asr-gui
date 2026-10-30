"""本地长音频 ASR 转写 GUI（tkinter）。

用途：驱动本目录下的 ``engine.py``，把长视频的音频转成文字。

特性：
  * 音频路径 / 引擎 / 设备 / 线程数 / 模型目录 / 输出位置，全部可在界面上选择；
  * 转写跑在后台线程，界面不卡死；停止按钮通过 ``threading.Event`` 优雅停止；
  * 进度、倍速、预计剩余时间实时刷新（数据经 ``queue.Queue`` 回主线程）；
  * 结果实时预览并同步追加写入 ``<名字>.txt``，``<名字>.jsonl`` 由引擎增量落盘，
    支持断点续跑。

依赖：仅标准库 tkinter + 本目录 engine.py，不新增任何 pip 依赖。
运行：``python app.py``（从任意 cwd 都可以）。
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

# ---- 保证从任意 cwd 运行都能 `import engine` --------------------------------
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from engine import CancelledError, Segment, Transcriber, format_timestamp

import config as app_config
import downloader
import models_registry as registry

# 引擎分块长度（秒），与 engine 默认一致
CHUNK_SECONDS = 300

# 模型清单由 models_registry 提供（8 档），界面上显示「标签｜缓存状态」。
# 所有 whisper 档位共用 engine.Transcriber 的 whisper 分支，只有 paraformer 走
# sherpa-onnx 分支。
ENDPOINT_LABELS = {
    "mirror": "hf-mirror 镜像（推荐）",
    "official": "Hugging Face 官方",
}
ENDPOINT_LABEL_TO_KEY = {v: k for k, v in ENDPOINT_LABELS.items()}
ENDPOINT_KEY_TO_LABEL = dict(ENDPOINT_LABELS)

MEDIA_FILE_TYPES = [
    ("媒体文件", "*.m4a *.mp3 *.wav *.flac *.aac *.ogg *.opus *.mp4 *.mkv *.mov *.webm"),
    ("音频文件", "*.m4a *.mp3 *.wav *.flac *.aac *.ogg *.opus"),
    ("所有文件", "*.*"),
]


def fmt_dur(seconds: float) -> str:
    """秒 -> ``HH:MM:SS``（不足 1 小时显示 ``MM:SS``）。"""
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def read_jsonl_segments(path: Path) -> list[Segment]:
    """读取已有 JSONL 结果（用于断点续跑时恢复预览与人读版 txt）。"""
    segments: list[Segment] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 崩溃时可能留下半行，跳过
                segments.append(
                    Segment(
                        start=float(item.get("start", 0.0)),
                        end=float(item.get("end", 0.0)),
                        text=str(item.get("text", "")),
                    )
                )
    except OSError:
        return []
    return segments


def last_end_seconds(segments: list[Segment]) -> float:
    """已有结果的最后结束时间（秒），即续跑起点。"""
    return max((s.end for s in segments), default=0.0)


class AsrGui:
    """主窗口逻辑。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("本地 ASR 长视频转写")
        self.root.geometry("1120x780")
        self.root.minsize(960, 640)
        try:
            self.root.option_add("*Font", ("Microsoft YaHei UI", 9))
        except tk.TclError:  # 某些环境没有该字体，忽略即可
            pass

        # ---- 跨线程通信：后台线程 -> 主线程 ----
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.cancel_event = threading.Event()
        self._dl_cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False          # 是否正在转写
        self.closing = False          # 收到关闭指令，等待 worker 收尾
        self.txt_handle = None        # 输出 txt 文件句柄（只在主线程使用）
        self.resume_base = 0.0        # 本次续跑的起点（秒）
        self.total_seconds = 0.0      # 媒体总时长（秒）

        # ---- 配置：首次运行自动生成，源码里不再写死任何本机路径 ----
        self.cfg = app_config.load()
        self.downloading = False
        self.label_to_id: dict = {}
        self.choices: list[registry.ModelChoice] = []

        # ---- 界面变量 ----
        self.var_media = tk.StringVar(value=str(self.cfg.get("last_media") or ""))
        self.var_model = tk.StringVar(value="")
        self.var_model_status = tk.StringVar(value="")
        self.var_model_path = tk.StringVar(value="")
        self.var_endpoint = tk.StringVar(
            value=ENDPOINT_KEY_TO_LABEL.get(
                str(self.cfg.get("endpoint") or "mirror"), ENDPOINT_LABELS["mirror"]
            )
        )
        self.var_device = tk.StringVar(value=str(self.cfg.get("device") or "cpu"))
        try:
            threads = int(self.cfg.get("num_threads") or 8)
        except (TypeError, ValueError):
            threads = 8
        self.var_threads = tk.IntVar(value=max(1, min(32, threads)))
        self.var_outdir = tk.StringVar(
            value=str(self.cfg.get("output_dir") or app_config.default_output_dir())
        )
        self.var_name = tk.StringVar(value=str(self.cfg.get("output_name") or "transcript"))
        self.var_status = tk.StringVar(value="就绪")
        self.var_hint = tk.StringVar(value="")

        self._build()
        self._refresh_models(initial_model=str(self.cfg.get("model_id") or registry.PARAFORMER_ID))
        self._log("界面就绪。先选模型：未缓存的点「下载模型」，已缓存的可直接开始。")
        self._log(f"模型缓存目录：{self.cfg.get('model_cache_dir')}")
        self._log(f"输出目录：{self.var_outdir.get()}")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll)

    # ===================================================================== UI

    def _build(self) -> None:
        root = self.root
        container = ttk.Frame(root, padding=10)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(4, weight=1)

        # ---------------------------------------------------- 输入与引擎
        f1 = ttk.LabelFrame(container, text="输入与引擎", padding=8)
        f1.grid(row=0, column=0, sticky="ew")
        f1.columnconfigure(1, weight=1)

        ttk.Label(f1, text="音频文件：").grid(row=0, column=0, sticky="w")
        self.ent_media = ttk.Entry(f1, textvariable=self.var_media, width=40)
        self.ent_media.grid(row=0, column=1, sticky="ew")
        self.btn_media = ttk.Button(f1, text="浏览…", command=self._browse_media)
        self.btn_media.grid(row=0, column=2, padx=(6, 0))

        ttk.Label(f1, text="模型：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.cmb_model = ttk.Combobox(
            f1, textvariable=self.var_model, values=[], state="readonly"
        )
        self.cmb_model.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        self.cmb_model.bind("<<ComboboxSelected>>", self._on_model_change)
        ttk.Label(f1, textvariable=self.var_model_status, foreground="#31708f").grid(
            row=1, column=3, columnspan=2, sticky="w", padx=(12, 0), pady=(8, 0)
        )

        ttk.Label(f1, text="设备：").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.cmb_device = ttk.Combobox(
            f1, textvariable=self.var_device, values=["cpu", "cuda"], state="readonly", width=10
        )
        self.cmb_device.grid(row=2, column=1, sticky="w", pady=(8, 0))

        ttk.Label(f1, text="线程数：").grid(row=2, column=2, sticky="w", padx=(12, 0))
        self.spin_threads = ttk.Spinbox(
            f1, from_=1, to=32, textvariable=self.var_threads, width=6
        )
        self.spin_threads.grid(row=2, column=3, sticky="w", pady=(8, 0))

        ttk.Label(f1, textvariable=self.var_hint, foreground="#8a6d3b").grid(
            row=3, column=0, columnspan=5, sticky="w", pady=(6, 0)
        )

        # ---------------------------------------------------- 模型文件与下载
        f2 = ttk.LabelFrame(container, text="模型文件", padding=8)
        f2.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        f2.columnconfigure(1, weight=1)

        ttk.Label(f2, text="模型位置：").grid(row=0, column=0, sticky="w")
        self.ent_model_path = ttk.Entry(f2, textvariable=self.var_model_path, width=40)
        self.ent_model_path.grid(row=0, column=1, sticky="ew")
        self.btn_model_locate = ttk.Button(
            f2, text="定位/导入…", command=self._on_locate_model
        )
        self.btn_model_locate.grid(row=0, column=2, padx=(6, 0))
        self.btn_model_download = ttk.Button(f2, text="下载模型", command=self._on_download_model)
        self.btn_model_download.grid(row=0, column=3, padx=(6, 0))

        ttk.Label(f2, text="下载源：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.cmb_endpoint = ttk.Combobox(
            f2,
            textvariable=self.var_endpoint,
            values=list(ENDPOINT_LABELS.values()),
            state="readonly",
            width=24,
        )
        self.cmb_endpoint.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.btn_refresh_models = ttk.Button(
            f2, text="重新检测缓存", command=lambda: self._refresh_models()
        )
        self.btn_refresh_models.grid(row=1, column=2, padx=(6, 0), pady=(8, 0))
        ttk.Label(
            f2,
            text=f"缓存目录：{self.cfg.get('model_cache_dir')}",
            foreground="#666666",
            wraplength=1010,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---------------------------------------------------- 输出
        f3 = ttk.LabelFrame(container, text="输出", padding=8)
        f3.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        f3.columnconfigure(1, weight=1)

        ttk.Label(f3, text="输出目录：").grid(row=0, column=0, sticky="w")
        self.ent_outdir = ttk.Entry(f3, textvariable=self.var_outdir, width=40)
        self.ent_outdir.grid(row=0, column=1, sticky="ew")
        self.btn_outdir = ttk.Button(
            f3, text="浏览…", command=lambda: self._browse_dir(self.var_outdir)
        )
        self.btn_outdir.grid(row=0, column=2, padx=(6, 0))

        ttk.Label(f3, text="文件名：").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.ent_name = ttk.Entry(f3, textvariable=self.var_name, width=24)
        self.ent_name.grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(f3, text="产出 <名字>.jsonl（断点续跑用）与 <名字>.txt（人读版）").grid(
            row=1, column=2, sticky="w", padx=(12, 0)
        )

        # ---------------------------------------------------- 控制 + 进度
        f4 = ttk.LabelFrame(container, text="控制与进度", padding=8)
        f4.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        f4.columnconfigure(2, weight=1)

        self.btn_start = ttk.Button(f4, text="开始转写", command=self.on_start)
        self.btn_start.grid(row=0, column=0, sticky="w")
        self.btn_stop = ttk.Button(f4, text="停止", command=self.on_stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(f4, textvariable=self.var_status).grid(row=0, column=2, sticky="w", padx=(12, 0))

        self.progress = ttk.Progressbar(f4, mode="determinate", maximum=100.0)
        self.progress.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.lbl_detail = ttk.Label(f4, text="—")
        self.lbl_detail.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ---------------------------------------------------- 日志 + 预览
        pane = ttk.PanedWindow(container, orient="horizontal")
        pane.grid(row=4, column=0, sticky="nsew", pady=(8, 0))

        left = ttk.LabelFrame(pane, text="日志", padding=4)
        right = ttk.LabelFrame(pane, text="识别结果预览", padding=4)
        pane.add(left, weight=1)
        pane.add(right, weight=2)

        self.txt_log = ScrolledText(
            left, wrap="word", width=30, height=12, state="disabled"
        )
        self.txt_log.pack(fill="both", expand=True)
        self.txt_preview = ScrolledText(
            right, wrap="word", width=40, height=12, state="disabled"
        )
        self.txt_preview.pack(fill="both", expand=True)

    # ============================================================ 交互回调

    def _browse_media(self) -> None:
        path = filedialog.askopenfilename(title="选择音频文件", filetypes=MEDIA_FILE_TYPES)
        if path:
            self.var_media.set(path)

    def _browse_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(title="选择目录")
        if path:
            var.set(path)

    # ============================================================ 模型选择 / 下载

    def _refresh_models(self, initial_model: str = "") -> None:
        """重新扫描模型缓存目录，重建 8 档模型下拉框并刷新状态。"""
        root_text = str(self.cfg.get("model_cache_dir") or "").strip()
        root = Path(root_text) if root_text else app_config.discover_cache_dir()
        try:
            self.choices = registry.list_choices(root)
        except Exception as exc:  # 扫描失败不该炸界面
            self.choices = []
            self._log(f"扫描模型缓存失败：{exc}")
        labels: list[str] = []
        self.label_to_id = {}
        for choice in self.choices:
            label = f"{choice.label}｜{choice.status_text}"
            labels.append(label)
            self.label_to_id[label] = choice.model_id
        self.cmb_model.configure(values=labels)

        want = initial_model or self._current_model_id() or registry.PARAFORMER_ID
        matched = ""
        for label, model_id in self.label_to_id.items():
            if model_id == want:
                matched = label
                break
        if not matched and labels:
            matched = labels[0]
        self.var_model.set(matched)
        self._apply_model_state()

    def _current_model_id(self) -> str:
        return self.label_to_id.get(self.var_model.get(), "")

    def _current_choice(self):
        model_id = self._current_model_id()
        for choice in self.choices:
            if choice.model_id == model_id:
                return choice
        return None

    def _manual_path(self, model_id: str) -> str:
        manual = self.cfg.get("manual_models")
        if isinstance(manual, dict):
            return str(manual.get(model_id) or "")
        return ""

    def _resolve_model_paths(self) -> tuple[str, str, str]:
        """返回 (引擎常量, sherpa 目录, whisper 目录)。"""
        choice = self._current_choice()
        model_id = choice.model_id if choice is not None else registry.PARAFORMER_ID
        location = self._manual_path(model_id) or (
            str(choice.location or "") if choice is not None else ""
        )
        if model_id == registry.PARAFORMER_ID:
            return Transcriber.ENGINE_PARAFORMER, location, ""
        # 所有 faster-whisper 档位共用引擎的 whisper 分支
        return Transcriber.ENGINE_BASE, "", location

    def _apply_model_state(self) -> None:
        """按当前模型刷新位置、状态与设备可用性。"""
        choice = self._current_choice()
        if choice is None:
            self.var_model_status.set("未检测到模型")
            self.var_model_path.set("")
            return
        engine_id, sherpa_dir, whisper_dir = self._resolve_model_paths()
        self.var_model_path.set(sherpa_dir or whisper_dir)
        self.var_model_status.set(choice.status_text)
        if engine_id == Transcriber.ENGINE_PARAFORMER:
            self.var_device.set("cpu")
            self.cmb_device.configure(state="disabled")
            self.var_hint.set("提示：paraformer-zh 仅支持 CPU，设备已锁定 cpu。")
        else:
            self.cmb_device.configure(state="readonly")
            self.var_hint.set("提示：whisper 可用 cuda（需 CUDA 12 运行库），失败就改回 cpu。")
        ready = bool(choice.cached) or bool(self._manual_path(choice.model_id))
        self.btn_model_download.configure(text="打开目录" if ready else "下载模型")

    def _on_model_change(self, _event=None) -> None:
        self._apply_model_state()
        choice = self._current_choice()
        if choice is not None:
            self._log(f"已选择模型：{choice.label}（{choice.status_text}）")

    def _on_locate_model(self) -> None:
        """手动指定已下载好的模型目录（离线/自定义缓存位置）。"""
        model_id = self._current_model_id()
        if not model_id:
            return
        path = filedialog.askdirectory(title="选择已下载的模型目录")
        if not path:
            return
        directory = Path(path)
        if model_id == registry.PARAFORMER_ID:
            ok, problems = registry.validate_manual_sherpa(directory)
        else:
            ok, problems = registry.validate_manual_whisper(directory, model_id)
        if not ok:
            detail = "\n".join(problems) if problems else "文件不完整或缺失"
            messagebox.showerror("目录不可用", f"该目录不是完整的 {model_id} 模型：\n\n{detail}")
            return
        manual = self.cfg.get("manual_models")
        if not isinstance(manual, dict):
            manual = {}
            self.cfg["manual_models"] = manual
        manual[model_id] = str(directory)
        saved, message = app_config.save(self.cfg)
        self._log(f"已登记模型目录：{model_id} -> {directory}")
        if not saved:
            self._log(f"配置保存失败：{message}")
        self._refresh_models(model_id)

    def _on_download_model(self) -> None:
        choice = self._current_choice()
        if choice is None or self.downloading:
            return
        ready = bool(choice.cached) or bool(self._manual_path(choice.model_id))
        if ready:
            folder = self._manual_path(choice.model_id) or str(choice.location or "")
            if folder:
                self._open_folder(Path(folder))
            return
        if self.running:
            messagebox.showwarning("请先停止", "转写进行中，无法同时下载模型。")
            return
        endpoint = ENDPOINT_LABEL_TO_KEY.get(self.var_endpoint.get(), "mirror")
        root_text = str(self.cfg.get("model_cache_dir") or "").strip()
        root = Path(root_text) if root_text else app_config.discover_cache_dir()
        self.downloading = True
        self._dl_cancel.clear()
        self._log(f"开始下载：{choice.label}（来源 {ENDPOINT_LABELS.get(endpoint, endpoint)}）")
        self._log(f"保存到：{root}")
        self._set_running_ui(True)
        threading.Thread(
            target=self._download_worker,
            args=(choice.model_id, root, endpoint),
            daemon=True,
        ).start()

    def _download_worker(self, model_id: str, root: Path, endpoint: str) -> None:
        def on_progress(done: int, total: int, name: str) -> None:
            self.q.put(("model_progress", (done, total, name)))

        try:
            result = downloader.download_model(
                model_id, root, endpoint, on_progress, self._dl_cancel
            )
        except Exception as exc:  # 后台线程绝不能让异常逃逸
            self.q.put(("model_done", (False, f"下载异常：{exc}")))
            return
        if result.ok:
            self.q.put(("model_done", (True, f"下载完成：{result.location}")))
        else:
            self.q.put(("model_done", (False, f"下载失败：{result.error or '未知错误'}")))

    def _handle_model_progress(self, done: int, total: int, name: str) -> None:
        done = int(done or 0)
        total = int(total or 0)
        if total > 0:
            pct = min(100.0, done * 100.0 / total)
            self.progress.configure(mode="determinate", maximum=100.0, value=pct)
            self.lbl_detail.configure(
                text=(
                    f"下载模型：{name}　{pct:.1f}%　"
                    f"{done / 1048576:.1f} / {total / 1048576:.1f} MB"
                )
            )
            self.var_status.set(f"下载模型 {pct:.1f}%")
        else:
            self.lbl_detail.configure(text=f"下载模型：{name}　{done / 1048576:.1f} MB")
            self.var_status.set("下载模型…")

    def _handle_model_done(self, ok: bool, message: str) -> None:
        self.downloading = False
        self._set_running_ui(False)
        self._log(message)
        self.progress.configure(mode="determinate", maximum=100.0, value=0.0)
        self.lbl_detail.configure(text="—")
        self.var_status.set("就绪")
        if ok:
            self._refresh_models(self._current_model_id())
        else:
            messagebox.showerror("下载失败", message)

    def _open_folder(self, path: Path) -> None:
        target = path if path.is_dir() else path.parent
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except Exception as exc:
            self._log(f"打开目录失败：{exc}")

    def _save_cfg_from_ui(self) -> None:
        self.cfg["model_id"] = self._current_model_id() or registry.PARAFORMER_ID
        self.cfg["device"] = self.var_device.get().strip() or "cpu"
        try:
            self.cfg["num_threads"] = int(self.var_threads.get())
        except (tk.TclError, ValueError):
            self.cfg["num_threads"] = 8
        self.cfg["endpoint"] = ENDPOINT_LABEL_TO_KEY.get(self.var_endpoint.get(), "mirror")
        self.cfg["output_dir"] = self.var_outdir.get().strip()
        self.cfg["output_name"] = self.var_name.get().strip()
        self.cfg["last_media"] = self.var_media.get().strip()
        saved, message = app_config.save(self.cfg)
        if not saved:
            self._log(f"配置保存失败：{message}")

    # ============================================================ 开始 / 停止

    def on_start(self) -> None:
        if self.running:
            return

        # ---------------- 前置校验（只有这里用 messagebox） ----------------
        media = self.var_media.get().strip()
        if not media or not Path(media).is_file():
            messagebox.showerror("无法开始", f"音频文件不存在：\n{media}")
            return

        name = self.var_name.get().strip()
        if not name:
            messagebox.showerror("无法开始", "请填写输出文件名。")
            return

        outdir = Path(self.var_outdir.get().strip())
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("无法开始", f"输出目录无法创建：\n{outdir}\n{exc}")
            return

        choice = self._current_choice()
        if choice is None:
            messagebox.showerror("无法开始", "请先选择模型。")
            return
        if not (choice.cached or self._manual_path(choice.model_id)):
            messagebox.showerror(
                "无法开始",
                f"模型「{choice.label}」尚未缓存。\n\n请先点「下载模型」，"
                "或用「定位/导入…」指定已下载好的模型目录。",
            )
            return

        engine_id, sherpa_dir, whisper_dir = self._resolve_model_paths()
        if engine_id == Transcriber.ENGINE_PARAFORMER:
            if not sherpa_dir or not Path(sherpa_dir).is_dir():
                messagebox.showerror("无法开始", f"sherpa 模型目录不存在：\n{sherpa_dir}")
                return
        else:
            if not whisper_dir or not Path(whisper_dir).is_dir():
                messagebox.showerror("无法开始", f"whisper 模型目录不存在：\n{whisper_dir}")
                return

        self._save_cfg_from_ui()

        try:
            threads = int(self.var_threads.get())
        except (tk.TclError, ValueError):
            threads = 8
        threads = max(1, min(32, threads))

        device = self.var_device.get().strip() or "cpu"
        if engine_id == Transcriber.ENGINE_PARAFORMER:
            device = "cpu"

        jsonl_path = outdir / f"{name}.jsonl"
        txt_path = outdir / f"{name}.txt"

        # ---------------- 断点续跑确认 ----------------
        existing = read_jsonl_segments(jsonl_path) if jsonl_path.is_file() else []
        mode = "fresh"
        if existing:
            resume_sec = last_end_seconds(existing)
            self._log(
                f"检测到已有结果，将从 {format_timestamp(resume_sec)} 继续"
                f"（{len(existing)} 段）。"
            )
            answer = messagebox.askyesnocancel(
                "断点续跑",
                f"检测到已有结果：{jsonl_path}\n"
                f"共 {len(existing)} 段，将从 {format_timestamp(resume_sec)} 继续。\n\n"
                "【是】继续转写\n【否】重新开始（清空 jsonl）\n【取消】不转写",
                default="yes",
            )
            if answer is None:
                self._log("用户取消了本次转写。")
                return
            mode = "resume" if answer else "restart"
            if mode == "restart":
                self._log("用户选择重新开始，将清空已有 jsonl。")
        else:
            self._log(f"未发现已有结果，将从头开始：{jsonl_path}")

        # ---------------- 准备输出文件 ----------------
        try:
            if mode == "restart":
                with open(jsonl_path, "w", encoding="utf-8"):
                    pass  # 清空 jsonl
                self._clear_widget(self.txt_preview)
                segments = []
            elif mode == "resume":
                segments = existing
            else:
                segments = []

            # txt 与 jsonl 保持同步：先按已有 jsonl 重建，再边转写边追加
            self.txt_handle = open(txt_path, "w", encoding="utf-8")
            for seg in segments:
                self._append_widget(self.txt_preview, self._segment_line(seg))
                self._write_txt(seg)
        except OSError as exc:
            messagebox.showerror("无法开始", f"输出文件无法写入：\n{exc}")
            self.txt_handle = None
            return

        self.resume_base = last_end_seconds(segments) if mode == "resume" else 0.0
        self.total_seconds = 0.0

        # ---------------- 启动后台线程 ----------------
        self.cancel_event.clear()
        self.running = True
        self.progress["value"] = 0.0
        self._set_running_ui(True)
        self._log(
            f"开始转写：引擎={engine_id}，设备={device}，线程={threads}，"
            f"媒体={media}"
        )
        self._log(f"输出：{jsonl_path} / {txt_path}")

        self.worker = threading.Thread(
            target=self._worker,
            args=(engine_id, sherpa_dir, whisper_dir, device, threads, media, str(jsonl_path)),
            name="asr-worker",
            daemon=True,
        )
        self.worker.start()

    def _worker(
        self,
        engine_id: str,
        sherpa_dir: str,
        whisper_dir: str,
        device: str,
        threads: int,
        media: str,
        jsonl_path: str,
    ) -> None:
        """后台线程：只做引擎相关的事，绝不碰 tkinter 控件。"""
        try:
            transcriber = Transcriber(
                engine=engine_id,
                sherpa_dir=sherpa_dir,
                whisper_dir=whisper_dir,
                device=device,
                num_threads=threads,
            )
            try:
                self.q.put(("log", f"正在加载模型（{engine_id}）…"))
                transcriber.load()
            except Exception as exc:  # 加载失败：属于"启动前"阶段，允许弹窗
                self.q.put(("load_error", str(exc)))
                return
            self.q.put(("log", "模型加载完成，开始转写…"))

            transcriber.run(
                media_path=media,
                output_jsonl=jsonl_path,
                chunk_seconds=CHUNK_SECONDS,
                progress_cb=lambda done, total, elapsed: self.q.put(
                    ("progress", (done, total, elapsed))
                ),
                segment_cb=lambda seg: self.q.put(("segment", seg)),
                cancel=self.cancel_event,
            )
            self.q.put(("done", None))
        except CancelledError:
            self.q.put(("cancelled", None))
        except Exception as exc:
            self.q.put(("error", f"{exc}\n{traceback.format_exc()}"))

    def on_stop(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        self._log("已请求停止，正在等待当前分块结束…")
        self.var_status.set("正在停止…")
        self.btn_stop.configure(state="disabled")

    # ============================================================ 关闭

    def on_close(self) -> None:
        self._save_cfg_from_ui()
        if self.downloading:
            self._dl_cancel.set()
        if self.running:
            ok = messagebox.askyesno(
                "正在转写",
                "当前正在转写。\n是否停止转写并退出？\n\n"
                "（已识别内容已经写入 jsonl / txt，下次可断点续跑）",
            )
            if not ok:
                return
            self.cancel_event.set()
            self.closing = True
            self._log("收到退出指令，正在优雅停止…")
            return
        self._destroy()

    def _destroy(self) -> None:
        self._close_txt()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ============================================================ 队列轮询

    def _poll(self) -> None:
        """主线程每 100 ms 取一次后台消息并刷新界面。"""
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            self._handle_message(item)

        if self.closing and not self.running:
            self._destroy()
            return
        try:
            self.root.after(100, self._poll)
        except tk.TclError:  # 窗口已销毁
            pass

    def _handle_message(self, item) -> None:
        """解析队列消息（必须是 (种类, 载荷) 二元组），解析失败只记日志不崩。"""
        try:
            kind, payload = item
        except (ValueError, TypeError):
            self._log(f"[内部错误] 无法解析的消息：{item!r}")
            return
        self._handle(kind, payload)

    def _handle(self, kind: str, payload) -> None:
        if kind == "log":
            self._log(str(payload))
        elif kind == "progress":
            done, total, elapsed = payload
            self._update_progress(done, total, elapsed)
        elif kind == "segment":
            seg: Segment = payload
            self._append_widget(self.txt_preview, self._segment_line(seg))
            self._write_txt(seg)
        elif kind == "done":
            self._on_finished("转写完成")
        elif kind == "cancelled":
            self._on_finished("已停止（可继续断点续跑）")
        elif kind == "load_error":
            self._log(f"模型加载失败：{payload}")
            self._on_finished("模型加载失败")
            messagebox.showerror(
                "模型加载失败",
                f"{payload}\n\n"
                "如果是 CUDA 相关报错，请把「设备」改回 cpu 后重试。",
            )
        elif kind == "error":
            self._log(f"运行出错：\n{payload}")
            self._on_finished("运行出错")
        elif kind == "model_progress":
            done, total, name = payload
            self._handle_model_progress(done, total, name)
        elif kind == "model_done":
            ok, message = payload
            self._handle_model_done(bool(ok), str(message))

    def _on_finished(self, state: str) -> None:
        self.running = False
        self._set_running_ui(False)
        self.var_status.set(state)
        self._close_txt()
        if state == "转写完成":
            self.progress["value"] = 100.0
            self._log("转写完成。")
        else:
            self._log(f"任务结束：{state}")

    def _set_running_ui(self, running: bool) -> None:
        """忙碌（转写或下载）期间禁用输入类控件，避免运行中被改动。"""
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(
            state="normal" if (running and not self.downloading) else "disabled"
        )
        widgets = [
            self.ent_media,
            self.btn_media,
            self.cmb_model,
            self.cmb_device,
            self.spin_threads,
            self.ent_model_path,
            self.btn_model_locate,
            self.btn_model_download,
            self.cmb_endpoint,
            self.btn_refresh_models,
            self.ent_outdir,
            self.btn_outdir,
            self.ent_name,
        ]
        for widget in widgets:
            widget.configure(state="disabled" if running else "normal")
        if not running:
            # Combobox 恢复只读；设备下拉的可用性再按模型类型修正
            self.cmb_model.configure(state="readonly")
            self.cmb_endpoint.configure(state="readonly")
            self.ent_model_path.configure(state="readonly")
            self._apply_model_state()

    # ============================================================ 进度显示

    def _update_progress(self, done: float, total: float, elapsed: float) -> None:
        total = float(total or 0.0)
        done = float(done or 0.0)
        elapsed = max(0.0, float(elapsed or 0.0))
        if total > 0:
            self.total_seconds = total

        # 续跑时 done 从已有进度开始，实际新增量要减掉续跑起点
        processed = max(0.0, done - self.resume_base)
        speed = processed / elapsed if elapsed > 0.0 else 0.0
        percent = (done / total * 100.0) if total > 0 else 0.0
        percent = max(0.0, min(100.0, percent))
        self.progress["value"] = percent

        total_text = fmt_dur(total) if total > 0 else "未知"
        if speed > 0.0:
            eta = max(0.0, total - done) / speed
            detail = (
                f"已处理 {fmt_dur(done)} / 总时长 {total_text}　"
                f"进度 {percent:.1f}%　倍速 {speed:.2f}×　"
                f"预计剩余 {fmt_dur(eta)}　已用 {fmt_dur(elapsed)}"
            )
        else:
            detail = (
                f"已处理 {fmt_dur(done)} / 总时长 {total_text}　"
                f"进度 {percent:.1f}%　倍速 —　预计剩余 —　已用 {fmt_dur(elapsed)}"
            )
        self.lbl_detail.configure(text=detail)
        self.var_status.set(
            f"转写中 {percent:.1f}%（{fmt_dur(done)} / {total_text}）"
        )

    # ============================================================ 文本与文件

    @staticmethod
    def _segment_line(seg: Segment) -> str:
        return f"[{format_timestamp(seg.start)}] {seg.text}\n"

    def _append_widget(self, widget: tk.Text, text: str) -> None:
        """向只读文本框追加内容（主线程调用）。"""
        try:
            widget.configure(state="normal")
            widget.insert("end", text)
            widget.see("end")
            widget.configure(state="disabled")
        except tk.TclError:
            pass

    def _clear_widget(self, widget: tk.Text) -> None:
        try:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")
        except tk.TclError:
            pass

    def _write_txt(self, seg: Segment) -> None:
        """把一段结果追加写入人读版 txt（主线程调用）。"""
        if self.txt_handle is None:
            return
        try:
            self.txt_handle.write(self._segment_line(seg))
            self.txt_handle.flush()
        except OSError as exc:
            self._log(f"写入 txt 失败：{exc}")

    def _close_txt(self) -> None:
        if self.txt_handle is not None:
            try:
                self.txt_handle.flush()
                self.txt_handle.close()
            except OSError:
                pass
            self.txt_handle = None

    def _log(self, message: str) -> None:
        """往日志区追加一行（主线程调用）。"""
        self._append_widget(self.txt_log, message.rstrip("\n") + "\n")


def _install_excepthooks() -> None:
    """把未捕获异常写到 logs/ 下的日志文件，避免 GUI 静默退出、方便用户回报问题。"""
    log_dir = Path(__file__).resolve().parent.parent / "logs"

    def _write(text: str) -> None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"error-{datetime.now():%Y%m%d}.log"
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
                handle.write(text)
        except OSError:
            pass

    def hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _write(text)
        try:
            messagebox.showerror(
                "程序出错",
                f"{exc_type.__name__}: {exc_value}\n\n详细堆栈已写入 logs/。",
            )
        except tk.TclError:
            pass

    def thread_hook(args) -> None:
        text = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        _write(f"[线程 {args.thread.name if args.thread else '?'}]\n{text}")

    sys.excepthook = hook
    threading.excepthook = thread_hook


def main() -> None:
    """启动 GUI。"""
    _install_excepthooks()
    root = tk.Tk()
    AsrGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
