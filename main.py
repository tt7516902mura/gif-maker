import base64, os, secrets, shutil, subprocess, tempfile, threading
from pathlib import Path
import fitz
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image, ImageOps
from starlette.background import BackgroundTask

PW = os.getenv("APP_PASSWORD", "")
MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "150"))
MAX_SIDE = 1920
ASPECT = {"rect": (16, 9), "square": (1, 1), "tall": (9, 16), "classic": (4, 3)}
LOCK = threading.Lock()
app = FastAPI()


@app.middleware("http")
async def guard(req, call_next):
    if int(req.headers.get("content-length") or 0) > MAX_MB * 1024 * 1024:
        return Response("ファイルが大きすぎます", 413)
    if PW:
        h = req.headers.get("authorization", "")
        try:
            ok = h[:6].lower() == "basic " and secrets.compare_digest(
                base64.b64decode(h[6:]).decode().partition(":")[2].encode(), PW.encode())
        except Exception:
            ok = False
        if not ok:
            return Response("認証が必要です", 401, headers={"WWW-Authenticate": 'Basic realm="gif"'})
    return await call_next(req)


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "index.html").read_text("utf-8")


def run(cmd):
    try:
        p = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *cmd],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "時間がかかりすぎました。長さや大きさを小さくしてください")
    if p.returncode:
        raise HTTPException(500, "ffmpegエラー: " + p.stderr[-400:])


def size(preset, w, h, src):
    w = max(16, min(w, MAX_SIDE))
    if preset == "custom":
        W, H = w, max(16, min(h, MAX_SIDE))
    else:
        a, b = ASPECT.get(preset) or src
        W, H = w, round(w * b / a)
    m = max(W, H)
    if m > MAX_SIDE:
        W, H = round(W * MAX_SIDE / m), round(H * MAX_SIDE / m)
    return max(W, 16), max(H, 16)


def fit(W, H, mode):
    if mode == "crop":
        return f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H}"
    return (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=white")


def place(im, W, H, mode):
    if mode == "crop":
        return ImageOps.fit(im, (W, H), Image.LANCZOS)
    im = ImageOps.contain(im, (W, H), Image.LANCZOS)
    c = Image.new("RGB", (W, H), "white")
    c.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    return c


def load_img(p):
    im = ImageOps.exif_transpose(Image.open(p)).convert("RGBA")
    bg = Image.new("RGBA", im.size, "white")
    bg.alpha_composite(im)
    return bg.convert("RGB")


def page_list(s, n):
    if not s.strip():
        return list(range(n))[:100]
    out = []
    try:
        for part in s.replace(" ", "").split(","):
            a, _, b = part.partition("-")
            out += [i - 1 for i in range(int(a), int(b or a) + 1) if 1 <= i <= n]
    except ValueError:
        raise HTTPException(400, "ページの指定が正しくありません(例: 1-3,5)")
    if not out:
        raise HTTPException(400, "指定したページがありません")
    return out[:100]


def palette_gif(inp, vf, tmp, out, loop):
    pal = tmp / "pal.png"
    run([*inp, "-vf", vf + ",palettegen=stats_mode=full", "-update", "1", str(pal)])
    run([*inp, "-i", str(pal), "-lavfi",
         f"{vf}[x];[x][1:v]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
         "-loop", "0" if loop else "-1", str(out)])


def build(kind, paths, tmp, out, preset, width, height, mode, fps, start, length, seconds, pages, loop):
    if kind == "video":
        first = tmp / "first.png"
        run(["-ss", str(start), "-i", str(paths[0]), "-frames:v", "1", str(first)])
        with Image.open(first) as im:
            W, H = size(preset, width, height, im.size)
        inp = ["-ss", str(start), "-t", str(min(length or 120, 120)), "-i", str(paths[0])]
        vf = f"fps={min(max(fps, 1), 30)},{fit(W, H, mode)}"
    else:
        if kind == "images":
            n, s = len(paths), load_img(paths[0]).size
            W, H = size(preset, width, height, s)
            get = lambda i: place(load_img(paths[i]), W, H, mode)
        else:
            doc = fitz.open(paths[0])
            idx = page_list(pages, len(doc))
            r = doc[idx[0]].rect
            W, H = size(preset, width, height, (r.width, r.height))
            n = len(idx)

            def get(i):
                r = doc[idx[i]].rect
                z = (max if mode == "crop" else min)(W / r.width, H / r.height)
                pix = doc[idx[i]].get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
                return place(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), W, H, mode)
        for i in range(n):
            get(i).save(tmp / f"f{i:04d}.png")
        inp = ["-framerate", f"{1 / max(seconds, 0.1):.5f}", "-i", str(tmp / "f%04d.png")]
        vf = "null"
    palette_gif(inp, vf, tmp, out, loop)
    return W, H


@app.post("/api/convert")
def convert(kind: str = Form(...), files: list[UploadFile] = File(...), preset: str = Form("rect"),
            width: int = Form(720), height: int = Form(720), fit_mode: str = Form("contain"),
            fps: int = Form(12), start: float = Form(0), length: float = Form(0),
            seconds: float = Form(2), pages: str = Form(""), loop: bool = Form(True)):
    if kind not in ("video", "images", "pdf"):
        raise HTTPException(400, "種類が正しくありません")
    tmp = Path(tempfile.mkdtemp())
    try:
        paths = []
        for i, f in enumerate(files[:100]):
            p = tmp / f"in{i}{Path(f.filename or '').suffix.lower()[:6]}"
            with open(p, "wb") as o:
                shutil.copyfileobj(f.file, o)
            paths.append(p)
        out = tmp / "out.gif"
        with LOCK:
            W, H = build(kind, paths, tmp, out, preset, width, height, fit_mode, fps,
                         max(start, 0), length, seconds, pages, loop)
    except HTTPException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(400, f"変換できませんでした: {e}")
    return FileResponse(out, media_type="image/gif", filename="output.gif",
                        headers={"X-Size": f"{W}x{H}"},
                        background=BackgroundTask(shutil.rmtree, tmp, True))
