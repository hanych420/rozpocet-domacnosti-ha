"""Bounded, offline ticket scanner, run in a separate process by app.py."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image, ImageOps
from pyzbar.pyzbar import decode, ZBarSymbol

MAX_PAGES = 12
Image.MAX_IMAGE_PIXELS = 20_000_000


def scan(source, mime_type, output_dir):
    results, seen = [], set()
    pages = 1
    if mime_type == "application/pdf":
        info = subprocess.run(["pdfinfo", str(source)], capture_output=True, check=True, timeout=10)
        for line in info.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("Pages:"):
                pages = int(line.split(":", 1)[1].strip())
                break
        if not 1 <= pages <= MAX_PAGES:
            raise ValueError("PDF může mít nejvýše 12 stran.")
    with tempfile.TemporaryDirectory(prefix="haneva-pages-") as temp:
        for page in range(1, pages + 1):
            if mime_type == "application/pdf":
                prefix = str(Path(temp) / "page")
                subprocess.run(["pdftoppm", "-png", "-scale-to", "2400", "-f", str(page),
                                "-l", str(page), "-singlefile", str(source), prefix],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                image_path = prefix + ".png"
            else:
                image_path = source
            with Image.open(image_path) as raw:
                image = ImageOps.exif_transpose(raw).convert("RGB")
                symbols = sorted(decode(image, symbols=[ZBarSymbol.QRCODE]),
                                 key=lambda item: (item.rect.top, item.rect.left))
                for symbol in symbols:
                    if not symbol.data or symbol.data in seen:
                        continue
                    seen.add(symbol.data)
                    if len(seen) > 50:
                        raise ValueError("V jednom souboru lze zpracovat nejvýše 50 různých QR kódů.")
                    x, y, width, height = symbol.rect
                    padding = max(20, int(max(width, height) * .16))
                    # Paste on white rather than crop outside the image (which adds black).
                    crop = image.crop((max(0, x-padding), max(0, y-padding),
                                       min(image.width, x+width+padding), min(image.height, y+height+padding)))
                    side = max(crop.size) + 40
                    canvas = Image.new("RGB", (side, side), "white")
                    canvas.paste(crop, ((side-crop.width)//2, (side-crop.height)//2))
                    scale = max(1, (900 + side - 1) // side)
                    canvas = canvas.resize((side*scale, side*scale), Image.Resampling.NEAREST)
                    # Verify the displayed crop really contains only this ticket's code.
                    if {item.data for item in decode(canvas, symbols=[ZBarSymbol.QRCODE])} != {symbol.data}:
                        raise ValueError("QR kódy jsou příliš blízko sebe nebo nečitelné. Použij původní PDF ve vyšší kvalitě.")
                    filename = f"qr-{len(results)}.png"
                    canvas.save(Path(output_dir) / filename, "PNG")
                    results.append({"file": filename, "digest": hashlib.sha256(symbol.data).hexdigest(), "page": page})
    if not results:
        raise ValueError("V souboru se nepodařilo najít čitelný QR kód. Zkus původní PDF nebo ostřejší obrázek.")
    return results


if __name__ == "__main__":
    try:
        print(json.dumps({"codes": scan(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))}))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
    except Exception:
        print(json.dumps({"error": "Soubor se nepodařilo přečíst. Zkus původní neuzamčené PDF se vstupenkami."}))
        sys.exit(1)
