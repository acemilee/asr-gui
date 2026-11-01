# 踩坑与根因记录（lessons）

> 详细根因/证据放这里，HANDOFF.md 只留指针（AGENTS 9.4）。

## 1. VideoToNo 打包版长视频卡死 = 64 KiB 管道死锁（2026-10-30 确证）

**现象**：短视频正常，9 小时以上长视频进度恒钉 38%，`_app.log` 不再增长，GPU 仍 75% 占用，任务既不失败也不完成。

**根因链**（上游 `github.com/like-attract/video-to-note`，main sha `868dcc3`）：

- `backend/asr_process.py:33-36` `use_child_process()` **只在 frozen(exe) 时为真** → 源码模式不走这条路径，所以只在打包版复现。
- `backend/asr_process.py:94-100` 建管道时**未设 `limit=`** → asyncio `StreamReader` 默认单行上限 64 KiB。
- `backend/asr_process.py:301` 工人把**整段转录塞进一行 JSON**（`:217-221` json.dumps 不换行）。
- `backend/asr_process.py:108-115` `pump()` **无 try/except** → 读行抛 `LimitOverrunError` / `ValueError: Separator is not found, and chunk exceed the limit` 后静默死掉，哨兵永不送达。
- `backend/asr_process.py:127` 父进程 `await events.get()` **无超时** → 永久阻塞；`:185-196` 非取消路径 `await process.wait()` 同样无超时，全流程无 watchdog。

**本机复现证据**（python 3.13）：

- `proc.stdout.readline()` 在单行 > 64 KiB 时抛 `ValueError: Separator is not found, and chunk exceed the limit`；
- 60,000 B 通过 / 70,000 B 永久阻塞；`limit=16*1024*1024` 时 400 KB、900 KB 正常；
- 体积对照：17 分钟 `transcript.json` = 22,105 B（paraformer）/ 64,536 B（whisper base，**已贴 64 KiB 线**）；10 小时 ×35.3 倍 → **780 KB ~ 2.28 MB**，必炸。
- 上游 issue #6 用 23 分钟即可复现，**当前 main 未修**。

**若要修 App 本体**：管道加 `limit=16*1024*1024`；`pump()` 加 try/except/finally 保证哨兵；父循环加超时。

**次要根因**：整段 `decode_audio` 无分片（`whisper_asr.py:232`、`paraformer_asr.py:219`，10 h ≈ 2.07 GB float32）；paraformer 引擎全程无进度行；LLM 笔记阶段 6500 字切块 + 单请求 300 s 超时 + **无任务级超时**；yt-dlp 无 socket_timeout/retries。

**已排除**：项目无任何云端 Whisper API（"25 MB 限制"不适用）；音频提取是 PyAV 不是 ffmpeg CLI。

## 2. numpy 逐帧 concatenate = O(n²)，解码慢 150 倍（自己写的引擎踩的）

第一版 `engine.py::_iter_chunks` 每来一帧就 `buf = np.concatenate([buf, samples])`。
300 秒一块 = 480 万采样点，帧数 ~4700 → 累计拷贝 ~45 GB → **解码 RTF 0.217（300 s 音频要 65 s）**，实测 10 小时要多花 40 分钟。

**改法**：预分配 `np.empty(chunk_samples, float32)`，逐帧写入，满一块就 yield 并换新缓冲。
改完 **解码 RTF 0.0014**（900 s 音频 1.3 s），快了 **150 倍**。

> 教训：分块流式处理里，"把数据攒起来"要用预分配缓冲，不要用 concatenate。

## 3. PyAV `container.seek(offset, stream=...)` 的 offset 单位是**流的 time_base**

用 `container.seek(int(sec * 1_000_000), stream=stream)`（AV_TIME_BASE 的值当流时间基用）→ **直接 seek 到文件末尾**（本例 chunk_start 变成 35990 而不是 600）。

**正确写法**：

```python
tb = stream.time_base
offset = int(start_sec / float(tb)) if tb else int(start_sec * 1_000_000)
container.seek(offset, stream=stream, backward=True)
```

不给 `stream=` 时才用 AV_TIME_BASE（微秒）。两种写法在本文件上都能定位，但前者更快（0.4 s 到 18000 s 处）。

## 4. 自研 ASR 的模型路径与上游 App 的一致性

上游 App 把模型缓存在 `<workspace>/_model_cache/sherpa/{asr,punc,vad}`，界面下拉里的 "paraformer-zn（已缓存）" 指的就是这批文件；**exe 里的 sherpa_onnx 只存在于 PyInstaller 解包目录**（`%TEMP%\_MEIxxxx`，内嵌 Python 3.11），所以 venv 里必须自己 `pip install sherpa-onnx==1.13.8`（有 cp313 wheel）才能在 Python 侧直接调 paraformer。

paraformer-zh 的 ONNX 含 `fsmn_block`（流式结构）→ **只能 CPU**。

## 5. 官方 B站缓存 vs yt-dlp 下载产物

- **yt-dlp 产物**（`audio_p1.m4a`）：干净 MP4，PyAV 直读。
- **B站桌面客户端缓存**（`%USERPROFILE%\Videos\bilibili\<uid>\*.m4s`）：**不是强加密，是头部 9 字节扰动**（文件头 9 个 ASCII `000000000`，真实 ftyp 在偏移 9）。ffprobe/PyAV 直读报 `Invalid data found when processing input`；剥 9 字节后 ftyp(32)→moov(805)→sidx(86152)→moof/mdat 完全正常。
- 结论：官方缓存能还原，但没必要碰它，workspace 里的 m4a 已等价可用。

## 6. 短切片基准会高估长片吞吐：持续满载后降频 3–4 倍（2026-10-30 实测）

冷机时用 120 s 切片测 paraformer-zn（threads=16）得 RTF **0.026**（3.1–3.5 s/120 s），按此外推 10 小时只要 ~15 分钟。
但**真实跑完整 10 小时（119 块 × 300 s）用了 64.8 分钟**，整片 RTF **0.108**（32.4 s/块），比外推慢 4 倍。

**验证**：转写结束、机器空闲（LoadPercentage 3%）后，用**同一段 120 s 音频**再测 3 次 → 9.57 / 12.50 / 12.43 s（RTF 0.080 / 0.104 / 0.104），与整片均值 0.108 吻合。
→ 结论：**变慢来自持续满载后的整机降频/功耗限制**（i9-13980HX 笔记本），不是代码问题，也不是那段切片内容特殊。

> 教训：单次短任务基准不能代表长时吞吐。估算长任务时间要用"冷机短测"与"长片实测"两个值，并以长片实测为准（本次 10 h = 60–70 分钟）。

## 7. Windows .bat 必须纯 ASCII + CRLF：LF-only 会让 cmd 解析崩掉（2026-10-30 踩）

用户双击 `asr_gui\启动转写GUI.bat` 报出一串无意义错误（`'鑵?GUI' 不是内部或外部命令`、`'~dp0"'`、`'" ('`、`系统找不到指定的路径。`）。

**字节级自证**：文件 366 B、首 10 字节 `40 65 63 68 6f 20 6f 66 66 0a`（`@echo off` + **纯 LF**，无 BOM），`CR=0 / LF=13`，且正文含 UTF-8 中文。

**根因**：①cmd.exe 按 OEM 代码页（GBK）读 .bat，UTF-8 中文变乱码；②**LF-only 换行会打乱 `if (...)` 复合块的解析**，块内 `%~dp0`、`(` 等碎片被当作独立命令执行——这就是 `'~dp0"'`、`'" ('` 的来源。

**规则**：仓库里所有 `.bat` 一律 纯 ASCII + CRLF（`CR == LF`、`nonAscii == 0`），中文只出现在 `.md`/`.py` 里；`-c "..."` 内联 Python 串**不能出现 `%`**（cmd 会当变量展开吞掉，曾导致 `SyntaxError: unterminated string literal`）。

## 8. tkinter 布局被 `ScrolledText` 默认 width 撑爆 + 截图要 DPI-aware（2026-10-30 踩）

**布局**：`ScrolledText` 默认 `width=80`（字符），放进 `PanedWindow` 后把 `winfo_reqwidth` 顶到 **1211 px**，而窗口只有 1120 宽 → 右侧控件被裁。改为 `width=30/40` 后 req 降到 **858**。诊断方法是打印每个 `Labelframe` 的 `winfo_reqwidth()` 逐个排查，而不是凭肉眼猜。

**截图**：显示器缩放 150% 时，DPI-unaware 的 Python 进程里 Tk 的 `winfo_width()`（1120 逻辑）与物理像素（1680）不一致，`ImageGrab(bbox=winfo_*)` 只能截到窗口左上 2/3，看起来像"窗口被裁了"，其实布局没问题。正确姿势：脚本**第一行** `ctypes.windll.user32.SetProcessDPIAware()`，再用 `GetWindowRect(int(root.wm_frame(), 16))` 作为 bbox。窗口被别的程序挡在前台时，`SetForegroundWindow` 常失败，用 Tk 自己的 `root.attributes("-topmost", True)` 才可靠；若连屏幕抓取都不行，`PrintWindow(hwnd, hdc, 2)` 可在窗口被遮挡时照样渲染。

## 9. PyInstaller 打包用错解释器：打进别人家的依赖树（2026-10-31 踩，最贵的一次）

**事故**：给 exe 新增「视频链接」来源（多一个 `yt_dlp`）后重建，产物从 109 MB **涨到 588 MB**，且运行时 `av`/`sherpa_onnx`/`ctranslate2`/`faster_whisper` 全部 `ModuleNotFoundError`。

**根因不在 yt-dlp**：`packaging\build.bat` 的解释器发现顺序是 `ASR_PYTHON` → `..\.venv` → **PATH 上的 `python.exe`**。本机没有 `.venv`，于是它挑了系统 Python（`F:\python313`）——那个环境里装满了 tensorflow / transformers / langchain / cv2 等无关重量级包，PyInstaller 顺着某条可选 import 把整棵依赖树拖进了产物（`tensorflow\python\_pywrap_tensorflow_common.dll` 一个文件就 944.6 MB），同时又**没有**本项目真正需要的 av / sherpa_onnx / ctranslate2。

**识别信号**：exe 自检里 `numpy 2.2.6`、`tokenizers 0.22.2`（系统 Python 的版本）而 venv 是 2.5.4 / 0.23.3 —— **自检打印的依赖版本是判断"exe 是用哪个解释器打的"最直接的证据**。之前 109 MB 那次能正常，是因为我手动用 venv 的 python 直接调了 `-m PyInstaller`，绕过了 `build.bat` 的发现逻辑。

**定位体积的方法**：`build\asr-gui\Analysis-00.toc` 里每个 `PYMODULE`/`BINARY` 都是绝对路径，按顶层包名聚合大小即可一眼看出谁在膨胀（一行 python 就能统计）。

**规则**：①打包脚本必须在动手前**校验解释器依赖**（本项目已在 `build.bat` 加 `[0/3]` 预检：缺 `numpy/av/sherpa_onnx/ctranslate2/faster_whisper/tokenizers/yt_dlp/PyInstaller` 就直接报错退出，并提示 `ASR_PYTHON`），绝不允许"找不到 venv 就静默用 PATH 上的 python"；②产物大小突变（>1.5 倍）必须当成 bug 查，不能当"加了功能所以变大"接受；③`collect_all()` 会把包的元数据/可选依赖一起收，纯 Python 包（如 yt-dlp）用 `collect_submodules()` 更干净。

## 10. 给第三方库传回调：参数名也算接口，别只测位置参数（2026-11-01 踩）

**事故**：v0.1.3 给 yt-dlp 配了 `retry_sleep_functions={"http": _retry_backoff, …}`，签名写成 `_retry_backoff(attempt)`。
第一次真正触发重试时直接炸：`TypeError: _retry_backoff() got an unexpected keyword argument 'n'`，
整次下载被判失败——**加固版比原版还容易失败**，因为原来 3 次重试不会走到退避逻辑，现在 10 次必然会走到。

**根因**：yt-dlp 内部是按**关键字**调用的：`yt_dlp/utils/_utils.py` 的
`RetryManager.report_retry()` 里写着 `delay = float_or_none(sleep_func(n=count - 1))`。
参数名必须叫 `n`（且要容忍多余关键字），跟"第几次重试"这个语义无关。

**为什么单测没抓到**：测试里写的是 `options["retry_sleep_functions"]["http"](4)`（位置参数），
而库用的是关键字 —— 位置参数在 Python 里照样能过。**教训**：给外部库的回调，测试必须**按库的调用方式**写
（关键字），或者干脆跑一次真实链路。

**廉价前置校验（最终采用）**：起一个本地 HTTP 服务，前两次请求**只发一半字节然后断连**，
再让 `sources.download_audio()` 去下 —— 真实 yt-dlp 会打印
`Got error: Downloaded 400022 bytes, expected 800044 bytes. Retrying (1/10)... Sleeping 1.00 seconds ...`
然后续传成功（hits=3、耗时 1.49 s、文件字节数正确）。这条 40 行脚本比"再等用户复现一次"便宜得多。

**规则**：①回调用关键字参数名对齐库实现（读库源码，别猜）；②涉及重试/退避这类"平时不走到"的分支，
必须有能真实触发它的测试，而不是只测配置字典；③加固一件事之后，要问一句"这条新路径第一次执行会在哪炸"。

## 11. 用 PowerShell 改写文档差点毁掉整份文件

**现象**：想给 HANDOFF.md 顶部插一段提交记录，写了
「读入文件内容 → 用 `-replace` 拼新字符串 → `Set-Content` 写回」的 PowerShell 管道。
`-replace` 因参数个数报错（`The -replace operator allows only two elements to follow it, not 3`），
目标变量变成 `$null`，`Set-Content` 把文件写成 **0 字节**。而 HANDOFF.md 被 `.gitignore` 排除，
**没有 git 副本可回滚**，只能整篇重写。

**教训**：
1. 改文档/代码一律用 write / edit 工具，不要用 PowerShell 变量管道 + `Set-Content`。
2. 非要用脚本，先写临时文件、校验非空、再 `Move-Item -Force` 覆盖。
3. 重要且未跟踪的文档（HANDOFF、AGENTS）应有副本或纳入版本控制。
