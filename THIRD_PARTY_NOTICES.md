# Third-Party Notices

`asr-gui` is released under the MIT license (see [LICENSE](LICENSE)). It would not exist without
the projects below. This file records the attribution obligations and the parts that were
referenced or reused; it is not legal advice.

## 1. Upstream inspiration — `like-attract/video-to-note`

- Project: <https://github.com/like-attract/video-to-note>
- License: MIT
- Copyright: Copyright (c) 2026 like-attract

This repository's engine is an **independent rewrite**. No source file from the upstream project is
copied into `asr_gui/`: decoding, VAD, recognition, punctuation, checkpointing and cancellation are
implemented from scratch against sherpa-onnx / faster-whisper / PyAV. What *was* taken from the
upstream project is factual knowledge, not code:

- the model inventory and its Hugging Face repo mapping (`Systran/faster-whisper-*`,
  `deepdml/faster-whisper-large-v3-turbo-ct2`,
  `wolfofbackstreet/faster-whisper-belle-whisper-large-v3-turbo-zh-ct2-int8`,
  `csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14`, the ct-transformer punctuation model and the
  Silero VAD model);
- the known failure mode of its packaged build — a 64 KiB subprocess pipe buffer deadlock that stalls
  multi-hour transcriptions — which motivated the single-process design here.

Because the upstream project is MIT-licensed and its model registry information was used, its
copyright notice and permission notice are reproduced in full below.

```text
MIT License

Copyright (c) 2026 like-attract

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 2. `sherpa-onnx`

- Project: <https://github.com/k2-fsa/sherpa-onnx>
- License: Apache License 2.0
- Use in this project: offline ASR inference for `paraformer-zh`, Silero VAD, and the
  ct-transformer punctuation model (via the PyPI wheel `sherpa-onnx==1.13.8`).

Apache-2.0 requires preservation of copyright and license notices and states that derivative
distributions must carry a copy of the license. If you redistribute a bundle that includes
sherpa-onnx binaries, include the upstream `LICENSE` file and this attribution.

## 3. `faster-whisper`

- Project: <https://github.com/SYSTRAN/faster-whisper>
- License: MIT
- Use in this project: the whisper recognition path (CPU and CUDA via CTranslate2), consumed as a
  runtime dependency (`faster-whisper>=1.2,<2`).

## 4. `CTranslate2`

- Project: <https://github.com/OpenNMT/CTranslate2>
- License: MIT
- Use in this project: inference runtime underneath faster-whisper; also the format of the
  `faster-whisper-*-ct2` model conversions downloaded by the app.

If you redistribute binaries, note that CTranslate2 may link against CUDA / cuBLAS / cuDNN
libraries, which are governed by NVIDIA's own license terms and are **not** covered by this
project's MIT license.

## 5. `PyAV`

- Project: <https://github.com/PyAV-Org/PyAV>
- License: BSD 3-Clause
- Use in this project: streaming chunked decode of video/audio containers, resampling to 16 kHz mono
  (`av>=13,<20`). PyAV wheels bundle FFmpeg builds, which carry their own (L)GPL terms depending on
  the build; check the wheel's notices before redistributing.

## 6. `tkinter` / Python standard library

- Project: <https://www.python.org/>
- License: Python Software Foundation License (BSD-style)
- Use in this project: the entire GUI layer. No third-party GUI toolkit (Qt, Electron, etc.) is used.

## 7. Model weights

Model weights are **not** included in this repository. They are downloaded on demand from Hugging
Face (or its mirror) and cached locally. Each weight repository defines its own license, terms of
use and acceptable-use policy; the user must comply with them. In particular:

| Tier | Repository | Upstream maintainer |
| --- | --- | --- |
| `tiny` / `base` / `small` / `medium` / `large-v3` | `Systran/faster-whisper-*` | Systran (CTranslate2 conversions of OpenAI Whisper weights) |
| `turbo` | `deepdml/faster-whisper-large-v3-turbo-ct2` | deepdml |
| `belle-turbo-zh` | `wolfofbackstreet/faster-whisper-belle-whisper-large-v3-turbo-zh-ct2-int8` | wolfofbackstreet (int8 CT2 conversion of BELLE-Whisper) |
| `paraformer-zh` | `csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14`, `csukuangfj/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12`, `csukuangfj/vad` | csukuangfj (sherpa-onnx conversions of FunASR Paraformer / ct-transformer, plus Silero VAD) |

The base Whisper weights originate from OpenAI and the Chinese variants from their respective
authors (BELLE, FunASR); the repositories above are conversions, not new grants of rights. No model
weights are redistributed with this project, so no model license text is reproduced here — consult
the linked repositories before any commercial or redistribution use.

## 8. Acknowledgements for data, not code

The performance figures in the READMEs were measured on a privately recorded long video. No media,
transcripts or personal data from it are part of this repository (`out/`, `*.jsonl`, `*.txt` and all
model caches are git-ignored).
