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
import pipeline
import sources

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

# 输入来源：视频链接 / 本地文件 / 字幕文件。字幕直接出稿，不需要模型。
SOURCE_URL = "视频链接"
SOURCE_FILE = "本地文件"
SOURCE_SUB = "字幕文件"
SOURCE_CHOICES = [SOURCE_URL, SOURCE_FILE, SOURCE_SUB]
SOURCE_HINTS = {
    SOURCE_URL: "粘贴 B 站 / 抖音 / YouTube 等视频页链接，只取音频，不转码",
    SOURCE_FILE: "本地音频或视频文件，常见格式都能解码",
    SOURCE_SUB: "直接读 .srt / .vtt 出稿，不用模型，几乎瞬间完成",
}
SUBTITLE_FILE_TYPES = [("字幕文件", "*.srt *.vtt"), ("所有文件", "*.*")]

MEDIA_FILE_TYPES = [
    ("媒体文件", "*.m4a *.mp3 *.wav *.flac *.aac *.ogg *.opus *.mp4 *.mkv *.mov *.webm"),
    ("音频文件", "*.m4a *.mp3 *.wav *.flac *.aac *.ogg *.opus"),
    ("所有文件", "*.*"),
]


def fmt_bytes(size: float) -> str:
    """把字节数变成人看得懂的大小。"""
    value = float(size or 0.0)
    if value < 1024.0:
        return f"{value:.0f} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024.0 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


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


def _read_jsonl_triples(path: Path) -> list[tuple[float, float, str]]:
    """读 ``{"start","end","text"}`` 形式的分段文件；缺文件/坏行都跳过。"""
    rows: list[tuple[float, float, str]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    start = float(item.get("start") or 0.0)
                    end = float(item.get("end") or start)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                text = str(item.get("text") or "").strip()
                if text:
                    rows.append((start, end, text))
    except OSError:
        return rows
    rows.sort(key=lambda row: row[0])
    return rows


class AsrGui:
    """主窗口逻辑。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"本地 ASR 长视频转写 v{app_config.APP_VERSION}")
        self.root.geometry("1120x980")
        self.root.minsize(960, 720)
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
        self._grand_total = 0.0       # 链接模式：所有分 P 加起来的时长
        self._engine_args: tuple | None = None   # 后台线程里构造引擎用
        self._transcriber = None      # 懒加载，多分 P 只加载一次模型
        self._page_offsets: list[tuple[int, float]] = []  # [(分 P 号, 时间轴偏移)]
        self._txt_path: Path | None = None
        self._jsonl_path: Path | None = None
        self._cookies_cache: dict[str, str] | None = None

        # ---- 配置：首次运行自动生成，源码里不再写死任何本机路径 ----
        self.cfg = app_config.load()
        self.downloading = False
        self.label_to_id: dict = {}
        self.choices: list[registry.ModelChoice] = []

        # ---- 界面变量 ----
        source = str(self.cfg.get("source") or SOURCE_FILE)
        if source not in SOURCE_CHOICES:
            source = SOURCE_FILE
        self.var_source = tk.StringVar(value=source)
        self.var_media = tk.StringVar(value=str(self.cfg.get("last_media") or ""))
        self.var_media_label = tk.StringVar(value="音频/视频：")
        self.var_source_hint = tk.StringVar(value=SOURCE_HINTS[SOURCE_FILE])
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
        self.var_pages = tk.StringVar(value=str(self.cfg.get("pages") or ""))
        self.var_cookies = tk.StringVar(
            value=sources.COOKIE_BROWSER_LABELS.get(
                str(self.cfg.get("cookies_from_browser") or ""),
                sources.COOKIE_BROWSER_LABELS[""],
            )
        )
        self.var_status = tk.StringVar(value="就绪")
        self.var_hint = tk.StringVar(value="")

        self._build()
        self._on_source_change()
        self._refresh_models(initial_model=str(self.cfg.get("model_id") or registry.PARAFORMER_ID))
        self._log("界面就绪。先选模型：未缓存的点「下载模型」，已缓存的可直接开始。")
        self._log(f"模型缓存目录：{app_config.effective_cache_dir(self.cfg)}")
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

        ttk.Label(f1, text="来源：").grid(row=0, column=0, sticky="w")
        self.cmb_source = ttk.Combobox(
            f1, textvariable=self.var_source, values=SOURCE_CHOICES, state="readonly", width=10
        )
        self.cmb_source.grid(row=0, column=1, sticky="w")
        self.cmb_source.bind("<<ComboboxSelected>>", self._on_source_change)
        ttk.Label(f1, textvariable=self.var_source_hint, foreground="#31708f").grid(
            row=0, column=2, columnspan=3, sticky="w", padx=(12, 0)
        )

        ttk.Label(f1, textvariable=self.var_media_label).grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        self.ent_media = ttk.Entry(f1, textvariable=self.var_media, width=40)
        self.ent_media.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        self.btn_media = ttk.Button(f1, text="浏览…", command=self._browse_media)
        self.btn_media.grid(row=1, column=2, padx=(6, 0), pady=(8, 0))

        ttk.Label(f1, text="模型：").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.cmb_model = ttk.Combobox(
            f1, textvariable=self.var_model, values=[], state="readonly"
        )
        self.cmb_model.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        self.cmb_model.bind("<<ComboboxSelected>>", self._on_model_change)
        ttk.Label(f1, textvariable=self.var_model_status, foreground="#31708f").grid(
            row=2, column=3, columnspan=2, sticky="w", padx=(12, 0), pady=(8, 0)
        )

        ttk.Label(f1, text="设备：").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.cmb_device = ttk.Combobox(
            f1, textvariable=self.var_device, values=["cpu", "cuda"], state="readonly", width=10
        )
        self.cmb_device.grid(row=3, column=1, sticky="w", pady=(8, 0))

        ttk.Label(f1, text="线程数：").grid(row=3, column=2, sticky="w", padx=(12, 0))
        self.spin_threads = ttk.Spinbox(
            f1, from_=1, to=32, textvariable=self.var_threads, width=6
        )
        self.spin_threads.grid(row=3, column=3, sticky="w", pady=(8, 0))

        # 仅在「视频链接」来源下出现的两项：分 P 范围与浏览器登录态
        self.lbl_pages = ttk.Label(f1, text="分 P：")
        self.lbl_pages.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.ent_pages = ttk.Entry(f1, textvariable=self.var_pages, width=16)
        self.ent_pages.grid(row=4, column=1, sticky="w", pady=(8, 0))
        self.lbl_pages_hint = ttk.Label(
            f1, text="留空 = 全部；例：1,3-5", foreground="#666666"
        )
        self.lbl_pages_hint.grid(row=4, column=2, sticky="w", padx=(6, 0), pady=(8, 0))

        self.lbl_cookies = ttk.Label(f1, text="登录态：")
        self.lbl_cookies.grid(row=4, column=3, sticky="e", pady=(8, 0))
        self.cmb_cookies = ttk.Combobox(
            f1,
            textvariable=self.var_cookies,
            values=[label for _key, label in sources.COOKIE_BROWSERS],
            state="readonly",
            width=10,
        )
        self.cmb_cookies.grid(row=4, column=4, sticky="w", padx=(6, 0), pady=(8, 0))

        ttk.Label(f1, textvariable=self.var_hint, foreground="#8a6d3b").grid(
            row=5, column=0, columnspan=5, sticky="w", pady=(6, 0)
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
            text=f"缓存目录：{app_config.effective_cache_dir(self.cfg)}",
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
        self.btn_outdir_open = ttk.Button(
            f3, text="打开目录", command=self._open_output_dir
        )
        self.btn_outdir_open.grid(row=0, column=3, padx=(6, 0))

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
            left, wrap="word", width=30, height=8, state="disabled"
        )
        self.txt_log.pack(fill="both", expand=True)
        self.txt_preview = ScrolledText(
            right, wrap="word", width=40, height=8, state="disabled"
        )
        self.txt_preview.pack(fill="both", expand=True)

    # ============================================================ 交互回调

    def _browse_media(self) -> None:
        """按当前来源决定按钮行为：链接=从剪贴板粘贴，文件=选媒体，字幕=选字幕。"""
        source = self.var_source.get()
        if source == SOURCE_URL:
            try:
                text = self.root.clipboard_get().strip()
            except tk.TclError:
                self._log("剪贴板里没有文本，请手动粘贴链接。")
                return
            if text:
                url = sources.extract_url(text)
                self.var_media.set(url or text)
                if url:
                    self._log(f"已从剪贴板粘贴链接：{url}")
                else:
                    self._log("剪贴板里没找到 http(s) 链接，已原样填入，请检查。")
            return
        if source == SOURCE_SUB:
            path = filedialog.askopenfilename(title="选择字幕文件", filetypes=SUBTITLE_FILE_TYPES)
        else:
            path = filedialog.askopenfilename(title="选择音频/视频文件", filetypes=MEDIA_FILE_TYPES)
        if path:
            self.var_media.set(path)
            if not self.var_name.get().strip() or self.var_name.get().strip() == "transcript":
                self.var_name.set(Path(path).stem)

    def _on_source_change(self, _event=None) -> None:
        """切换来源：改标签文字、提示语，并按需要禁用「浏览…」。"""
        source = self.var_source.get()
        if source == SOURCE_SUB:
            self.var_media_label.set("字幕文件：")
            self.btn_media.configure(text="浏览…", state="normal")
        elif source == SOURCE_URL:
            self.var_media_label.set("视频链接：")
            self.btn_media.configure(text="粘贴", state="normal")
        else:
            self.var_media_label.set("音频/视频：")
            self.btn_media.configure(text="浏览…", state="normal")
        self.var_source_hint.set(SOURCE_HINTS.get(source, ""))
        self._apply_url_widgets(source == SOURCE_URL)
        if source == SOURCE_SUB:
            self.var_hint.set("提示：字幕文件直接出稿，模型与设备设置不参与。")
        else:
            self.var_hint.set("")
            self._apply_model_state()

    def _apply_url_widgets(self, visible: bool) -> None:
        """分 P / 登录态 只在「视频链接」来源下显示（其他来源用不上）。"""
        widgets = (
            self.lbl_pages,
            self.ent_pages,
            self.lbl_pages_hint,
            self.lbl_cookies,
            self.cmb_cookies,
        )
        for widget in widgets:
            if visible:
                widget.grid()
            else:
                widget.grid_remove()

    def _browse_dir(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(title="选择目录")
        if path:
            var.set(path)

    # ============================================================ 模型选择 / 下载

    def _refresh_models(self, initial_model: str = "") -> None:
        """重新扫描模型缓存目录，重建 8 档模型下拉框并刷新状态。"""
        root = app_config.effective_cache_dir(self.cfg)
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
        root = app_config.effective_cache_dir(self.cfg)
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

    def _open_output_dir(self) -> None:
        """打开「输出目录」（不存在就先建出来）。"""
        raw = self.var_outdir.get().strip()
        target = Path(raw) if raw else app_config.default_output_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log(f"创建输出目录失败：{exc}")
            return
        self._open_folder(target)

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
        self.cfg["source"] = self.var_source.get()
        self.cfg["last_media"] = self.var_media.get().strip()
        self.cfg["pages"] = self.var_pages.get().strip()
        self.cfg["cookies_from_browser"] = self._cookies_key()
        saved, message = app_config.save(self.cfg)
        if not saved:
            self._log(f"配置保存失败：{message}")

    # ============================================================ 开始 / 停止

    def on_start(self) -> None:
        if self.running:
            return

        # ---------------- 前置校验（只有这里用 messagebox） ----------------
        source = self.var_source.get()
        media = self.var_media.get().strip()
        if not media:
            messagebox.showerror("无法开始", "请先填写或选择输入内容。")
            return
        if source == SOURCE_URL:
            url = sources.extract_url(media)
            if not url:
                messagebox.showerror(
                    "无法开始",
                    "这段文本里没找到 http:// 或 https:// 开头的链接：\n"
                    f"{media[:200]}\n\n"
                    "B 站「复制分享」会把标题一起带过来，只保留链接即可。",
                )
                return
            if url != media:
                # 粘贴的是分享文案：只留链接，省得用户手动删标题
                self.var_media.set(url)
                media = url
                self._log(f"已从分享文案里取出链接：{url}")
        elif source == SOURCE_SUB:
            subtitle = Path(media)
            if not subtitle.is_file():
                messagebox.showerror("无法开始", f"字幕文件不存在：\n{media}")
                return
            if subtitle.suffix.lower() not in sources.SUBTITLE_SUFFIXES:
                messagebox.showerror(
                    "无法开始", f"只支持 .srt / .vtt 字幕文件：\n{subtitle.name}"
                )
                return
        elif not Path(media).is_file():
            messagebox.showerror("无法开始", f"音频/视频文件不存在：\n{media}")
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

        prepared = self._prepare_engine(source)
        if prepared is None:
            return
        engine_id, sherpa_dir, whisper_dir, device, threads = prepared

        self._save_cfg_from_ui()

        jsonl_path = outdir / f"{name}.jsonl"
        txt_path = outdir / f"{name}.txt"

        # ---------------- 断点续跑确认 ----------------
        reusable = jsonl_path.is_file() and source != SOURCE_SUB
        existing = read_jsonl_segments(jsonl_path) if reusable else []
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
        self._txt_path = txt_path
        self._jsonl_path = jsonl_path
        self._page_offsets = []

        # ---------------- 启动后台线程 ----------------
        self.cancel_event.clear()
        self.running = True
        self.progress["value"] = 0.0
        self._set_running_ui(True)
        if source == SOURCE_SUB:
            worker_target = self._subtitle_worker
            worker_args = (media, str(txt_path), str(jsonl_path))
            self._log(f"开始解析字幕：{media}")
        elif source == SOURCE_URL:
            worker_target = self._url_worker
            # Tk 变量只能在主线程读，所以先把分 P / 登录态取出来再交给后台线程
            worker_args = (
                media,
                engine_id,
                sherpa_dir,
                whisper_dir,
                device,
                threads,
                str(jsonl_path),
                self.var_pages.get().strip(),
                self._cookies_key(),
            )
            self._log(f"开始下载并转写链接：{media}")
        else:
            worker_target = self._worker
            worker_args = (
                engine_id,
                sherpa_dir,
                whisper_dir,
                device,
                threads,
                media,
                str(jsonl_path),
            )
            self._log(
                f"开始转写：引擎={engine_id}，设备={device}，线程={threads}，媒体={media}"
            )
        self._log(f"输出：{jsonl_path} / {txt_path}")

        self.worker = threading.Thread(
            target=worker_target,
            args=worker_args,
            name="asr-worker",
            daemon=True,
        )
        self.worker.start()

    def _prepare_engine(self, source: str):
        """校验模型是否可用并解析引擎参数；不可用时弹窗并返回 None。"""
        if source == SOURCE_SUB:
            # 字幕文件直接出稿，不加载任何模型
            return "", "", "", "cpu", 8

        choice = self._current_choice()
        if choice is None:
            messagebox.showerror("无法开始", "请先选择模型。")
            return None
        if not (choice.cached or self._manual_path(choice.model_id)):
            messagebox.showerror(
                "无法开始",
                f"模型「{choice.label}」尚未缓存。\n\n请先点「下载模型」，"
                "或用「定位/导入…」指定已下载好的模型目录。",
            )
            return None

        engine_id, sherpa_dir, whisper_dir = self._resolve_model_paths()
        if engine_id == Transcriber.ENGINE_PARAFORMER:
            if not sherpa_dir or not Path(sherpa_dir).is_dir():
                messagebox.showerror("无法开始", f"sherpa 模型目录不存在：\n{sherpa_dir}")
                return None
        elif not whisper_dir or not Path(whisper_dir).is_dir():
            messagebox.showerror("无法开始", f"whisper 模型目录不存在：\n{whisper_dir}")
            return None

        try:
            threads = int(self.var_threads.get())
        except (tk.TclError, ValueError):
            threads = 8
        threads = max(1, min(32, threads))

        device = self.var_device.get().strip() or "cpu"
        if engine_id == Transcriber.ENGINE_PARAFORMER:
            device = "cpu"
        return engine_id, sherpa_dir, whisper_dir, device, threads

    def _ensure_transcriber(self):
        """懒加载引擎：链接模式多个分 P 共用同一个模型实例，避免反复加载。"""
        if self._transcriber is not None:
            return self._transcriber
        if self._engine_args is None:
            self.q.put(("error", "内部错误：引擎参数没有初始化。"))
            return None
        engine_id, sherpa_dir, whisper_dir, device, threads = self._engine_args
        transcriber = Transcriber(
            engine=engine_id,
            sherpa_dir=sherpa_dir,
            whisper_dir=whisper_dir,
            device=device,
            num_threads=threads,
        )
        self.q.put(("log", f"正在加载模型（{engine_id}）…"))
        try:
            transcriber.load()
        except Exception as exc:  # 加载失败：属于"启动前"阶段，允许弹窗
            self.q.put(("load_error", str(exc)))
            return None
        self._transcriber = transcriber
        self.q.put(("log", "模型加载完成。"))
        return transcriber

    def _transcribe_media(self, media: Path, jsonl_path: Path, offset: float) -> bool:
        """转写一个媒体文件（pipeline 注入的回调）：段落按 ``offset`` 平移到总时间轴。"""
        transcriber = self._ensure_transcriber()
        if transcriber is None:
            return False
        try:
            transcriber.run(
                media_path=str(media),
                output_jsonl=str(jsonl_path),
                chunk_seconds=CHUNK_SECONDS,
                progress_cb=lambda done, total, elapsed: self.q.put(
                    ("progress", (offset + done, self._grand_total or total, elapsed))
                ),
                segment_cb=lambda seg: self.q.put(
                    (
                        "segment",
                        Segment(start=seg.start + offset, end=seg.end + offset, text=seg.text),
                    )
                ),
                cancel=self.cancel_event,
            )
            return True
        except CancelledError:
            self.q.put(("cancelled", None))
            return False
        except Exception as exc:
            self.q.put(("error", f"{exc}\n{traceback.format_exc()}"))
            return False

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
        """后台线程（本地文件）：只做引擎相关的事，绝不碰 tkinter 控件。"""
        try:
            self._engine_args = (engine_id, sherpa_dir, whisper_dir, device, threads)
            self._transcriber = None
            if self._ensure_transcriber() is None:
                return
            transcriber = self._transcriber
            self.q.put(("log", "开始转写…"))
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

    def _cookies_key(self) -> str:
        """把「登录态」下拉的文案反查回 yt-dlp 的浏览器代号（空串 = 不使用）。"""
        label = self.var_cookies.get().strip()
        for key, text in sources.COOKIE_BROWSERS:
            if text == label:
                return key
        return ""

    def _emit_segments(self, segments, offset: float) -> None:
        """把整段字幕按 ``offset`` 平移后推给界面（pipeline 注入的回调）。"""
        for seg in segments:
            if self.cancel_event.is_set():
                return
            self.q.put(
                (
                    "segment",
                    Segment(start=seg.start + offset, end=seg.end + offset, text=seg.text),
                )
            )

    def _url_worker(
        self,
        url: str,
        engine_id: str,
        sherpa_dir: str,
        whisper_dir: str,
        device: str,
        threads: int,
        jsonl_path: str,
        pages_spec: str = "",
        cookies_key: str = "",
    ) -> None:
        """后台线程：视频链接 → 分 P 拆解 → 字幕优先，缺字幕的才下载音频转写。"""
        transcript_dir = Path(jsonl_path).parent
        stem = Path(jsonl_path).stem
        try:
            downloads = app_config.downloads_dir()
            downloads.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.q.put(("error", f"下载目录无法创建：{exc}"))
            return

        self._engine_args = (engine_id, sherpa_dir, whisper_dir, device, threads)
        self._transcriber = None
        self._grand_total = 0.0
        self._page_offsets = []
        self.q.put(("log", f"下载目录：{downloads}"))

        cookies = sources.browser_cookies(
            cookies_key, log=lambda message: self.q.put(("log", message))
        )

        def on_progress(done: float, total: float, label: str) -> None:
            self._grand_total = float(total or 0.0)
            self.q.put(("progress", (done, total, 0.0)))
            if label:
                self.q.put(("phase", f"处理中：{label}"))

        try:
            result = pipeline.run_url_pipeline(
                url,
                transcript_dir=transcript_dir,
                stem=stem,
                downloads_dir=downloads,
                transcribe=self._transcribe_media,
                emit_segments=self._emit_segments,
                cookies=cookies,
                pages_spec=pages_spec,
                subtitle_first=True,
                log=lambda message: self.q.put(("log", message)),
                progress=on_progress,
                download_progress=lambda done, total, label: self.q.put(
                    ("download_progress", (done, total))
                ),
                cancel=self.cancel_event,
            )
        except Exception as exc:
            self.q.put(("error", f"{exc}\n{traceback.format_exc()}"))
            return

        if result.cancelled or self.cancel_event.is_set():
            self.q.put(("cancelled", None))
            return
        if not result.ok:
            self.q.put(("error", result.error or "下载或转写失败。"))
            return
        self._page_offsets = list(result.page_offsets)
        if result.subtitle_pages:
            self.q.put(("log", f"{result.subtitle_pages} 个分 P 直接用 B 站字幕出稿，未占用模型。"))
        self.q.put(("done", None))

    def _subtitle_worker(self, sub_path: str, txt_path: str, jsonl_path: str) -> None:
        """后台线程：直接解析字幕文件，按段落推给界面（不加载任何模型）。"""
        try:
            self.q.put(("log", f"正在解析字幕：{sub_path}"))
            segments = sources.parse_subtitle_file(sub_path)
            if not segments:
                self.q.put(("error", "字幕文件里没有解析出任何一句，请确认内容是 .srt / .vtt。"))
                return
            self.q.put(("log", f"共 {len(segments)} 段字幕，正在生成文本…"))
            sources.write_jsonl(jsonl_path, segments)
            for seg in segments:
                if self.cancel_event.is_set():
                    self.q.put(("cancelled", None))
                    return
                self.q.put(("segment", Segment(start=seg.start, end=seg.end, text=seg.text)))
            total = segments[-1].end
            self.q.put(("progress", (total, total, 0.0)))
            self.q.put(("done", None))
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
        elif kind == "download_progress":
            done, total = payload
            self._update_download_progress(done, total)
        elif kind == "phase":
            self.var_status.set(str(payload))
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
            self._rebuild_txt()
            self._log("转写完成。")
        else:
            self._log(f"任务结束：{state}")

    def _rebuild_txt(self) -> None:
        """按分 P 顺序把分段 jsonl 合并成一份完整 txt（链接模式的关键收尾）。

        只有单个媒体文件时，jsonl 里的时间轴已经是全局的，直接读那份即可；
        多分 P 时每个 P 各有一份 ``<名字>.p{N}.jsonl``（时间是 P 内的），
        这里用 pipeline 返回的偏移表把它们接回同一条时间轴。
        """
        if self._txt_path is None or self._jsonl_path is None:
            return
        rows: list[tuple[float, float, str]] = []
        sources_paths: list[tuple[Path, float]] = []
        if self._page_offsets:
            for page, offset in self._page_offsets:
                sources_paths.append(
                    (self._jsonl_path.parent / f"{self._jsonl_path.stem}.p{page}.jsonl", offset)
                )
        else:
            sources_paths.append((self._jsonl_path, 0.0))
        for path, offset in sources_paths:
            for start, end, text in _read_jsonl_triples(path):
                rows.append((start + offset, end + offset, text))
        if not rows:
            return
        rows.sort(key=lambda row: row[0])
        try:
            with open(self._txt_path, "w", encoding="utf-8") as handle:
                for start, _end, text in rows:
                    handle.write(f"[{format_timestamp(start)}] {text}\n")
        except OSError as exc:
            self._log(f"合并 txt 失败：{exc}")
            return
        self._clear_widget(self.txt_preview)
        for start, _end, text in rows:
            self._append_widget(
                self.txt_preview,
                f"[{format_timestamp(start)}] {text}\n",
            )
        self._log(f"已按时间轴重建完整文本：{self._txt_path}（{len(rows)} 段）")

    def _set_running_ui(self, running: bool) -> None:
        """忙碌（转写或下载）期间禁用输入类控件，避免运行中被改动。"""
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(
            state="normal" if (running and not self.downloading) else "disabled"
        )
        widgets = [
            self.cmb_source,
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
            self.ent_pages,
            self.cmb_cookies,
        ]
        for widget in widgets:
            widget.configure(state="disabled" if running else "normal")
        if not running:
            # Combobox 恢复只读；设备下拉的可用性再按模型类型修正
            self.cmb_model.configure(state="readonly")
            self.cmb_endpoint.configure(state="readonly")
            self.cmb_cookies.configure(state="readonly")
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

    def _update_download_progress(self, done: float, total: float) -> None:
        """下载阶段没有时间轴，只报字节进度，避免和转写进度混在一起。"""
        done = float(done or 0.0)
        total = float(total or 0.0)
        if total > 0:
            percent = max(0.0, min(100.0, done / total * 100.0))
            self.progress["value"] = percent
            detail = (
                f"下载中 {fmt_bytes(done)} / {fmt_bytes(total)}　进度 {percent:.1f}%"
            )
        else:
            self.progress["value"] = 0.0
            detail = f"下载中 {fmt_bytes(done)}　（服务器未提供总大小）"
        self.lbl_detail.configure(text=detail)
        self.var_status.set("下载音频中…")

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
    log_dir = app_config.logs_dir()

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
                f"{exc_type.__name__}: {exc_value}\n\n详细堆栈已写入：\n{log_dir}",
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


def _selftest_report() -> tuple[list[str], bool]:
    """收集自检信息：打包产物里最容易坏的是原生依赖能否加载。

    返回 (报告行, 是否有失败)。
    """
    failed = False
    lines = [f"asr-gui {app_config.APP_VERSION} 自检", f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}"]
    lines.append(f"frozen（是否是打包 exe）：{app_config.is_frozen()}")
    lines.append(f"python：{sys.version.split()[0]}  可执行文件：{sys.executable}")
    lines.append(f"程序目录：{app_config.app_home()}")
    lines.append(f"配置目录：{app_config.config_dir()}")
    lines.append(f"Crash 日志目录：{app_config.logs_dir()}")
    lines.append(f"默认输出目录：{app_config.default_output_dir()}")

    for name in (
        "numpy",
        "av",
        "sherpa_onnx",
        "ctranslate2",
        "tokenizers",
        "faster_whisper",
        "yt_dlp",
    ):
        try:
            module = __import__(name)
            # yt-dlp 不在顶层暴露 __version__，版本在 yt_dlp.version 里。
            version = getattr(module, "__version__", None)
            if version is None:
                version = getattr(getattr(module, "version", None), "__version__", "?")
            lines.append(f"依赖 {name}：OK ({version})")
        except Exception as exc:  # noqa: BLE001 - 自检要把任何异常都报出来
            failed = True
            lines.append(f"依赖 {name}：失败 -> {exc.__class__.__name__}: {exc}")

    root = app_config.effective_cache_dir(app_config.load())
    lines.append(f"模型缓存目录：{root}")
    try:
        for choice in registry.list_choices(root):
            lines.append(f"  模型 {choice.model_id}：{choice.status_text}")
    except OSError as exc:
        failed = True
        lines.append(f"  枚举模型失败：{exc}")

    # 真正加载一次 paraformer（若已缓存），验证原生库、VAD、标点模型都能起来。
    sherpa_dir = root / "sherpa"
    if (sherpa_dir / "asr" / "model.int8.onnx").is_file():
        try:
            transcriber = Transcriber(Transcriber.ENGINE_PARAFORMER, sherpa_dir=sherpa_dir)
            transcriber.load()
            lines.append("加载 paraformer-zh：OK（asr + punc + vad 均就绪）")
        except Exception as exc:  # noqa: BLE001
            failed = True
            lines.append(f"加载 paraformer-zh：失败 -> {exc.__class__.__name__}: {exc}")
    else:
        lines.append("加载 paraformer-zh：跳过（该目录下没有缓存的 sherpa 模型）")

    lines.append("")
    if failed:
        lines.append("结论：有项目未通过，请把本文件内容贴到 issue。")
    else:
        lines.append("结论：全部通过，可以正常转写。")
    return lines, failed


def _run_selftest(target: Path, interactive: bool) -> int:
    lines, failed = _selftest_report()
    text = "\n".join(lines)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    # 只有双击运行（没给输出文件参数）时才弹窗；给参数就是为了能被脚本/CI 调用。
    if interactive:
        try:
            messagebox.showinfo("asr-gui 自检", text)
        except tk.TclError:
            pass
    return 2 if failed else 0


def _resolve_source(source: str, log) -> "Path | None":
    """把命令行来源解析成本地媒体文件：URL 先下音频，其余当本地路径。"""
    url = sources.extract_url(source)
    if url:
        source = url
        log(f"下载音频：{source}")
        result = sources.download_audio(source, app_config.downloads_dir(), log=log)
        if not result.ok or result.path is None:
            log(f"下载失败：{result.error}")
            return None
        size_mb = result.path.stat().st_size / 1048576
        log(f"下载完成：{result.title}（{size_mb:.1f} MB）-> {result.path}")
        return result.path
    media = Path(source)
    if not media.is_file():
        log(f"找不到文件：{media}")
        return None
    return media


def _run_subtitle_cli(path: Path, out_txt: Path, log) -> int:
    """字幕文件直接出稿（不用模型）。"""
    try:
        segments = sources.parse_subtitle_file(path)
    except Exception as exc:  # noqa: BLE001 - 命令行模式把异常写进日志
        log(f"失败：{exc.__class__.__name__}: {exc}")
        return 1
    if not segments:
        log("字幕文件里没有解析出任何一句（需要 .srt / .vtt 内容）。")
        return 1
    jsonl_path = out_txt.with_suffix(".jsonl")
    sources.write_jsonl(jsonl_path, segments)
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    with out_txt.open("w", encoding="utf-8") as handle:
        for segment in segments:
            handle.write(f"[{format_timestamp(segment.start)}] {segment.text}\n")
    log(f"完成：{len(segments)} 段 -> {out_txt}")
    return 0


def _load_model_for_cli(model_id: str, device: str, threads: int, log) -> "Transcriber | None":
    """按模型 id 解析目录并加载引擎；返回 None 表示失败（原因已写日志）。"""
    cfg = app_config.load()
    cache_root = app_config.effective_cache_dir(cfg)
    choice = registry.choice_by_id(model_id, cache_root)
    if choice is None:
        log(f"未知模型：{model_id}")
        return None
    # 手动导入的目录优先（与界面里「定位/导入…」写入的值一致）。
    manual = cfg.get("manual_models") or {}
    manual_path = manual.get(model_id) if isinstance(manual, dict) else None
    location = Path(manual_path) if manual_path else Path(choice.location or "")
    if choice.kind == "sherpa":
        engine_id = Transcriber.ENGINE_PARAFORMER
        sherpa_dir, whisper_dir = location, Path()
        device = "cpu"  # paraformer 只能 CPU
    else:
        engine_id = Transcriber.ENGINE_BASE
        sherpa_dir, whisper_dir = Path(), location
    log(f"模型={choice.label}  文件={location}  设备={device}  线程={threads}")
    transcriber = Transcriber(
        engine_id,
        sherpa_dir=sherpa_dir,
        whisper_dir=whisper_dir,
        device=device,
        num_threads=threads,
    )
    transcriber.load()
    return transcriber


def _write_rows(out_txt: Path, rows: list[tuple[float, float, str]]) -> None:
    """按时间轴顺序写出人读版 txt。"""
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda row: row[0])
    with out_txt.open("w", encoding="utf-8") as handle:
        for start, _end, text in ordered:
            handle.write(f"[{format_timestamp(start)}] {text}\n")


def _run_url_cli(
    url: str,
    out_txt: Path,
    model_id: str,
    device: str,
    threads: int,
    pages: str,
    cookies_browser: str,
    cookiefile: str,
    subtitle_first: bool,
    log,
) -> int:
    """命令行：视频链接 → 分 P / 字幕优先 / 语音兜底，最后写一份完整 txt。"""
    downloads = app_config.downloads_dir()
    downloads.mkdir(parents=True, exist_ok=True)
    log(f"下载目录：{downloads}")

    cookies: dict[str, str] = {}
    if cookies_browser:
        cookies = sources.browser_cookies(cookies_browser, log=log)
    elif cookiefile:
        cookies = sources.load_cookie_file(cookiefile)
        log(f"已读取 cookie 文件：{len(cookies)} 个")
    elif not cookies:
        log("未使用登录态；B 站 AI 字幕通常需要登录态，拿不到会回退语音转写。")

    rows: list[tuple[float, float, str]] = []
    holder: dict = {"transcriber": None}

    def get_transcriber():
        if holder["transcriber"] is None:
            holder["transcriber"] = _load_model_for_cli(model_id, device, threads, log)
            if holder["transcriber"] is not None:
                log("模型已加载")
        return holder["transcriber"]

    def transcribe(media: Path, jsonl_path: Path, offset: float) -> bool:
        transcriber = get_transcriber()
        if transcriber is None:
            return False
        transcriber.run(
            str(media),
            str(jsonl_path),
            chunk_seconds=CHUNK_SECONDS,
            progress_cb=lambda done, total, elapsed: log(
                f"进度 {(done / total * 100) if total else 0:5.1f}%"
                f"  {format_timestamp(done)} / {format_timestamp(total)}"
            ),
        )
        for start, end, text in _read_jsonl_triples(jsonl_path):
            rows.append((start + offset, end + offset, text))
        return True

    def emit_segments(segments, offset: float) -> None:
        for seg in segments:
            rows.append((seg.start + offset, seg.end + offset, seg.text))

    result = pipeline.run_url_pipeline(
        url,
        transcript_dir=out_txt.parent,
        stem=out_txt.stem,
        downloads_dir=downloads,
        transcribe=transcribe,
        emit_segments=emit_segments,
        cookies=cookies,
        pages_spec=pages,
        subtitle_first=subtitle_first,
        cookies_from_browser=cookies_browser,
        cookiefile=cookiefile,
        log=log,
    )
    if result.cancelled:
        log("已取消")
        return 130
    if not result.ok:
        log(f"失败：{result.error}")
        return 1
    _write_rows(out_txt, rows)
    log(
        f"完成：{len(rows)} 段 -> {out_txt}"
        f"（{result.pages_done} 个分 P，其中 {result.subtitle_pages} 个直接用字幕）"
    )
    return 0


def _run_headless(
    source: str,
    out_txt: Path,
    model_id: str,
    device: str,
    threads: int,
    pages: str = "",
    cookies_browser: str = "",
    cookiefile: str = "",
    subtitle_first: bool = True,
) -> int:
    """命令行转写（不弹窗）：给批处理/服务器/自动化用，也方便打包后做端到端验证。

    ``source`` 可以是本地音频/视频文件、视频页链接（自动下音频，可指定分 P 与登录态），
    或 ``.srt`` / ``.vtt`` 字幕文件（直接出稿，不需要模型）。
    """
    log_path = out_txt.with_suffix(".log")
    lines: list[str] = []

    def log(message: str) -> None:
        stamp = f"[{datetime.now():%H:%M:%S}] "
        lines.append(stamp + message)
        try:
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    # 允许把「分享文案」整段粘进来
    url = sources.extract_url(source)
    if url:
        source = url

    try:
        is_subtitle = source.lower().endswith(sources.SUBTITLE_SUFFIXES)
        if is_subtitle and not sources.is_supported_url(source):
            return _run_subtitle_cli(Path(source), out_txt, log)
        if sources.is_supported_url(source):
            return _run_url_cli(
                source,
                out_txt,
                model_id,
                device,
                threads,
                pages,
                cookies_browser,
                cookiefile,
                subtitle_first,
                log,
            )

        transcriber = _load_model_for_cli(model_id, device, threads, log)
        if transcriber is None:
            return 2
        log("模型已加载，开始转写")
        jsonl_path = out_txt.with_suffix(".jsonl")

        def on_progress(done: float, total: float, elapsed: float) -> None:
            percent = (done / total * 100) if total else 0.0
            log(f"进度 {percent:5.1f}%  {format_timestamp(done)} / {format_timestamp(total)}")

        transcriber.run(
            source,
            jsonl_path,
            chunk_seconds=CHUNK_SECONDS,
            progress_cb=on_progress,
        )
        rows = _read_jsonl_triples(jsonl_path)
        _write_rows(out_txt, rows)
        log(f"完成：{len(rows)} 段 -> {out_txt}")
        return 0
    except CancelledError:
        log("已取消")
        return 130
    except Exception as exc:  # noqa: BLE001 - 命令行模式要把异常写进日志而不是崩掉
        log(f"失败：{exc.__class__.__name__}: {exc}")
        return 1


def _parse_transcribe_flags(args: list[str], cfg: dict, source: str) -> dict:
    """解析 ``--transcribe`` 后面的可选参数（缺省值全部来自配置文件）。"""
    model_id = str(cfg.get("model_id") or registry.PARAFORMER_ID)
    device = str(cfg.get("device") or "cpu")
    threads = int(cfg.get("num_threads") or 8)
    out_arg: str | None = None
    pages = str(cfg.get("pages") or "")
    cookies_browser = str(cfg.get("cookies_from_browser") or "")
    cookiefile = ""
    subtitle_first = True
    position = 0
    while position < len(args):
        flag = args[position]
        value = args[position + 1] if position + 1 < len(args) else ""
        if flag == "--out":
            out_arg = value
            position += 2
        elif flag == "--model":
            model_id = value
            position += 2
        elif flag == "--device":
            device = value
            position += 2
        elif flag == "--threads":
            threads = max(1, min(32, int(value or threads)))
            position += 2
        elif flag == "--pages":
            pages = value
            position += 2
        elif flag == "--cookies-from-browser":
            cookies_browser = value
            position += 2
        elif flag == "--cookies":
            cookiefile = value
            position += 2
        elif flag == "--no-subtitle-first":
            subtitle_first = False
            position += 1
        else:
            print(f"忽略未知参数：{flag}")
            position += 1
    if out_arg:
        out_txt = Path(out_arg)
    else:
        stem = "transcript" if sources.is_supported_url(source) else Path(source).stem
        out_txt = Path(str(cfg.get("output_dir"))) / ((stem or "transcript") + ".txt")
    return {
        "model_id": model_id,
        "device": device,
        "threads": threads,
        "out_txt": out_txt,
        "pages": pages,
        "cookies_browser": cookies_browser,
        "cookiefile": cookiefile,
        "subtitle_first": subtitle_first,
    }


def main() -> None:
    """启动 GUI。

    另外两种命令行模式（打包后的 exe 同样支持）：
      * ``--selftest [输出文件]``：环境自检，写报告并退出（0 通过 / 2 有失败）；
      * ``--transcribe 媒体 [--out 文本] [--model 模型id] [--device cpu|cuda] [--threads N]``：
        不弹窗的批量转写，日志写在输出文件旁边的 ``.log``。
    """
    argv = sys.argv[1:]
    if "--selftest" in argv:
        index = argv.index("--selftest")
        rest = argv[index + 1 :]
        target = Path(rest[0]) if rest else Path.home() / "asr-gui-selftest.txt"
        raise SystemExit(_run_selftest(target, interactive=not rest))

    if "--transcribe" in argv:
        index = argv.index("--transcribe")
        rest = argv[index + 1 :]
        if not rest:
            print("用法：--transcribe <本地文件|视频链接|.srt|.vtt> [--out <文本>] …")
            raise SystemExit(2)
        source = rest[0]
        cfg = app_config.load()
        parsed = _parse_transcribe_flags(rest[1:], cfg, source)
        raise SystemExit(
            _run_headless(
                source,
                parsed["out_txt"],
                parsed["model_id"],
                parsed["device"],
                parsed["threads"],
                pages=parsed["pages"],
                cookies_browser=parsed["cookies_browser"],
                cookiefile=parsed["cookiefile"],
                subtitle_first=parsed["subtitle_first"],
            )
        )

    _install_excepthooks()
    root = tk.Tk()
    AsrGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
