"""Render the ClipVault app icon (.ico) — same design as the in-app logo."""
import sys
from PIL import Image, ImageDraw


def render(size, a1=(255, 122, 200), a2=(127, 214, 255)):
    s = size * 4
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    grad = Image.new("RGBA", (s, s))
    px = grad.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2 * s)
            px[x, y] = tuple(int(a1[i] + (a2[i] - a1[i]) * t) for i in range(3)) + (255,)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([s * .06, s * .06, s * .94, s * .94], radius=s * .22, fill=255)
    im.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(im)
    d.ellipse([s * .22, s * .22, s * .78, s * .78], fill=(11, 11, 16, 255))
    d.polygon([(s * .44, s * .37), (s * .64, s * .5), (s * .44, s * .63)], fill=a1 + (255,))
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "clipvault.ico"
    big = render(256)
    big.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(out.replace(".ico", ".png"))
