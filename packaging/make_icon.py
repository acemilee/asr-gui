"""生成 packaging/icon.ico（发布用图标）。

只需要 Pillow，构建 exe 之前跑一次即可；生成的 .ico 已提交到仓库，所以普通用户
和 CI 都不需要装 Pillow。

    python packaging/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
BG = (23, 32, 42)  # #17202a 深色底
ACCENT = (79, 195, 247)  # #4fc3f7 波形
EDGE = (38, 52, 68)

# 五根竖条的相对高度（0..1），中间高两边低，像一段音量包络
BARS = (0.22, 0.46, 0.66, 0.46, 0.22)


def build() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = int(SIZE * 0.22)
    draw.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=radius, fill=BG, outline=EDGE, width=8)

    bar_width = int(SIZE * 0.085)
    gap = int(SIZE * 0.055)
    total = len(BARS) * bar_width + (len(BARS) - 1) * gap
    x = (SIZE - total) // 2
    center_y = SIZE // 2
    for height in BARS:
        bar_height = int(SIZE * height)
        top = center_y - bar_height // 2
        draw.rounded_rectangle(
            (x, top, x + bar_width, top + bar_height),
            radius=bar_width // 2,
            fill=ACCENT,
        )
        x += bar_width + gap
    return image


def main() -> None:
    target = Path(__file__).resolve().parent / "icon.ico"
    image = build()
    image.save(target, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
