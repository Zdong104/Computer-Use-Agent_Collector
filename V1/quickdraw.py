from PIL import Image, ImageDraw
from pathlib import Path
import json

base = Path('/home/zihan/Downloads/CUA_BehaviorClone/data/20260402_092539_7b8d34d5')
meta = json.loads((base / 'task.json').read_text())

action = meta['actions'][0]
img_path = base / 'screenshots' / action['pre_screenshot']
out_path = base / 'screenshots' / 'action_0001_before_clicked.png'

img = Image.open(img_path).convert('RGBA')
draw = ImageDraw.Draw(img)

img_w, img_h = img.size
src_w, src_h = action['screen_resolution']
x, y = action['action_coords']

# 把记录时的屏幕坐标映射到截图坐标
px = round(x * img_w / src_w)
py = round(y * img_h / src_h)
px = max(0, min(px, img_w - 1))
py = max(0, min(py, img_h - 1))

# 画点击标记
r = 28
for width, color in [(10, (255, 255, 255, 220)), (6, (255, 0, 0, 255))]:
    draw.ellipse((px-r, py-r, px+r, py+r), outline=color, width=width)

draw.line((px-r-18, py, px+r+18, py), fill=(255, 0, 0, 255), width=4)
draw.line((px, py-r-18, px, py+r+18), fill=(255, 0, 0, 255), width=4)

img.convert('RGB').save(out_path)
print(out_path)
print(f"Recorded coords: ({x}, {y}) on {src_w}x{src_h}")
print(f"Mapped coords:   ({px}, {py}) on {img_w}x{img_h}")
