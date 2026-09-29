"""Crop README screenshots to the card (plus padding) and resize the wide one."""
from PIL import Image, ImageChops
def crop(src, dst, width=None, pad=32):
    im = Image.open(src).convert("RGB")
    bg = Image.new("RGB", im.size, im.getpixel((2, im.height - 3)))  # page background
    box = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > 12 else 0).getbbox()
    l, t, r, b = box
    im = im.crop((max(0, l - pad), max(0, t - pad), min(im.width, r + pad), min(im.height, b + pad)))
    if width and im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    im.save(dst, optimize=True)
    print(dst, im.size)
crop("/out/hero.png", "/out/card-light.png", width=1760)
crop("/out/dark.png", "/out/card-dark.png")
