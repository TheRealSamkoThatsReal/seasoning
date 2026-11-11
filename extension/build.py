"""Build the browser extension for Chrome (also ChromeOS/Edge/Brave) and Firefox (also Zen).

    python extension/build.py          -> extension/dist/{chrome,firefox}/ + .zip files
"""
import json
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

VERSION = "0.2.1"
BASE = {
    "manifest_version": 3,
    "name": "seasoning",
    "version": VERSION,
    "description": "Snow, leaves and petals fall over web pages and pile up on their elements. "
                   "Syncs with the seasoning desktop app when it's running.",
    "icons": {s: f"icons/icon{s}.png" for s in ("16", "32", "48", "128")},
    "action": {"default_popup": "popup.html", "default_title": "seasoning",
               "default_icon": {s: f"icons/icon{s}.png" for s in ("16", "32")}},
    "permissions": ["storage", "activeTab"],
    "host_permissions": ["http://127.0.0.1:47914/*"],
    "content_scripts": [{"matches": ["http://*/*", "https://*/*", "file:///*"],
                         "js": ["content.js"], "run_at": "document_idle"}],
}


def icons(out):
    from seasoning.windows import _icon_image
    from PIL import Image
    big = _icon_image().resize((256, 256), Image.LANCZOS)
    bg = Image.new("RGBA", big.size, (0, 0, 0, 0))
    from PIL import ImageDraw
    ImageDraw.Draw(bg).ellipse([8, 8, 248, 248], fill=(19, 36, 64, 255))   # dark disc so it shows on light toolbars
    bg.alpha_composite(big.resize((200, 200), Image.LANCZOS), (28, 28))
    os.makedirs(os.path.join(out, "icons"), exist_ok=True)
    for s in (16, 32, 48, 128):
        bg.resize((s, s), Image.LANCZOS).save(os.path.join(out, "icons", f"icon{s}.png"))


def build(target):
    out = os.path.join(HERE, "dist", target)
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(os.path.join(HERE, "src"), out)
    icons(out)
    m = json.loads(json.dumps(BASE))
    if target == "chrome":
        m["background"] = {"service_worker": "background.js"}
    else:
        m["background"] = {"scripts": ["background.js"]}
        m["browser_specific_settings"] = {"gecko": {"id": "seasoning@therealsamkothatsreal.github.io",
                                                    "strict_min_version": "140.0",
                                                    "data_collection_permissions": {"required": ["none"]}}}
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(m, f, indent=2)
    # Firefox/Zen take the same zip named .xpi
    ext = "zip" if target == "chrome" else "xpi"
    z = os.path.join(HERE, "dist", f"seasoning-{target}-{VERSION}.{ext}")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for d, _, files in os.walk(out):
            for fn in files:
                p = os.path.join(d, fn)
                zf.write(p, os.path.relpath(p, out))
    return out, z


if __name__ == "__main__":
    for t in ("chrome", "firefox"):
        print(*build(t))
