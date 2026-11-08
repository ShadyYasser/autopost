"""Draw a preview of the frame (no video needed): python scripts/preview_frame.py out.png [WxH of the source]."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from autopost.frame import FrameStyle, render_layer, video_rect  # noqa: E402

root = Path(__file__).resolve().parents[1]
out = sys.argv[1] if len(sys.argv) > 1 else "frame-preview.png"
vw, vh = (int(v) for v in (sys.argv[2] if len(sys.argv) > 2 else "720x1280").split("x"))
style = FrameStyle(logo=root / "assets/kalima_sawaa/logo.png", credit="المصدر: معاذ عليان",
                   font=root / "assets/fonts/DejaVuSans-Bold.ttf")
rect = video_rect(style, vw, vh)
x, y, w, h = rect
fake = Image.new("RGB", (w, h))
d = ImageDraw.Draw(fake)
for i in range(h):                                   # dark gradient standing in for the video
    d.line([(0, i), (w, i)], fill=(24 + i * 30 // h, 30 + i * 20 // h, 48 + i * 40 // h))
canvas = Image.new("RGB", (style.width, style.height), style.bg)
canvas.paste(fake, (x, y))
layer = render_layer(style, rect)
canvas.paste(layer, (0, 0), layer)
canvas.save(out)
print(out, rect)
