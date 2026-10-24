"""Face matching (optional, off by default): who is on screen while a voice speaks?

Reference faces come from each person's dossier photo. Frames grabbed at a voice's turns (the same ones the
caption OCR uses) are scanned for faces; a face that matches someone's photo becomes evidence in the name
suggestions ("face matches Elizabeth Warren's photo"). Models: InsightFace buffalo_l (SCRFD detector +
ArcFace recogniser) as ONNX, run with onnxruntime; downloaded once from GitHub.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import zipfile
from pathlib import Path

import numpy as np

from . import config, db

log = logging.getLogger(__name__)
PACK_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
DET_FILE, REC_FILE = "det_10g.onnx", "w600k_r50.onnx"
STRONG, WEAK = 0.45, 0.32          # ArcFace cosine: same person usually > 0.45; below 0.32 is noise
# ArcFace 112x112 alignment template (left eye, right eye, nose, left mouth, right mouth)
TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)
_LOCK = threading.Lock()
_MODELS: dict = {}


def model_dir() -> Path:
    return config.MODEL_DIR / "faces"


def available() -> bool:
    return (model_dir() / DET_FILE).exists() and (model_dir() / REC_FILE).exists()


def download_models(progress=None) -> dict:
    """Fetch the buffalo_l pack (≈275 MB) and keep the two files we use."""
    import urllib.request
    model_dir().mkdir(parents=True, exist_ok=True)
    tmp = model_dir() / "buffalo_l.zip.part"
    req = urllib.request.Request(PACK_URL, headers={"User-Agent": "VoiceFlock"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0); done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk); done += len(chunk)
            if progress and total:
                progress("downloading face models", done / total)
    with zipfile.ZipFile(tmp) as z:
        for name in z.namelist():
            base = Path(name).name
            if base in (DET_FILE, REC_FILE):
                (model_dir() / base).write_bytes(z.read(name))
    tmp.unlink(missing_ok=True)
    return {"ok": available(), "dir": str(model_dir())}


def _sessions():
    with _LOCK:
        if "det" not in _MODELS:
            import onnxruntime as ort
            so = ort.SessionOptions(); so.intra_op_num_threads = 2
            _MODELS["det"] = ort.InferenceSession(str(model_dir() / DET_FILE), so, providers=["CPUExecutionProvider"])
            _MODELS["rec"] = ort.InferenceSession(str(model_dir() / REC_FILE), so, providers=["CPUExecutionProvider"])
        return _MODELS["det"], _MODELS["rec"]


# ---------------------------------------------------------------- detection (SCRFD, 640x640, strides 8/16/32, 2 anchors)

def detect(img: "np.ndarray", thresh: float = 0.5, size: int = 640) -> list[dict]:
    """img: HxWx3 RGB uint8 -> faces [{bbox:[x1,y1,x2,y2], kps:(5,2), score}] in image coordinates, largest first."""
    det, _ = _sessions()
    h, w = img.shape[:2]
    scale = size / max(h, w)
    nh, nw = int(round(h * scale)), int(round(w * scale))
    from PIL import Image
    resized = np.asarray(Image.fromarray(img).resize((nw, nh), Image.BILINEAR))
    canvas = np.zeros((size, size, 3), dtype=np.uint8); canvas[:nh, :nw] = resized
    blob = ((canvas[:, :, ::-1].astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]   # BGR, normalised
    outs = det.run(None, {det.get_inputs()[0].name: blob})
    faces = []
    strides = [8, 16, 32]
    n = len(outs) // 3
    for i, stride in enumerate(strides):
        scores = outs[i].reshape(-1)
        bboxes = outs[i + n].reshape(-1, 4) * stride
        kpss = outs[i + 2 * n].reshape(-1, 10) * stride if n >= 3 else None
        fh = fw = size // stride
        ys, xs = np.mgrid[:fh, :fw]
        centers = np.stack([xs.ravel(), ys.ravel()], axis=1).astype(np.float32) * stride
        centers = np.repeat(centers, 2, axis=0)   # two anchors per cell
        keep = np.where(scores >= thresh)[0]
        for k in keep:
            cx, cy = centers[k]
            x1, y1, x2, y2 = cx - bboxes[k, 0], cy - bboxes[k, 1], cx + bboxes[k, 2], cy + bboxes[k, 3]
            kps = None
            if kpss is not None:
                kp = kpss[k].reshape(5, 2); kps = np.stack([cx + kp[:, 0], cy + kp[:, 1]], axis=1) / scale
            faces.append({"bbox": [float(x1 / scale), float(y1 / scale), float(x2 / scale), float(y2 / scale)], "kps": kps, "score": float(scores[k])})
    faces = _nms(faces, 0.4)
    faces.sort(key=lambda f: -(f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]))
    return faces


def _nms(faces: list[dict], iou_thr: float) -> list[dict]:
    faces = sorted(faces, key=lambda f: -f["score"]); out = []
    for f in faces:
        x1, y1, x2, y2 = f["bbox"]; ok = True
        for g in out:
            gx1, gy1, gx2, gy2 = g["bbox"]
            iw, ih = max(0, min(x2, gx2) - max(x1, gx1)), max(0, min(y2, gy2) - max(y1, gy1))
            inter = iw * ih; union = (x2 - x1) * (y2 - y1) + (gx2 - gx1) * (gy2 - gy1) - inter
            if union > 0 and inter / union > iou_thr:
                ok = False; break
        if ok:
            out.append(f)
    return out


# ---------------------------------------------------------------- recognition (ArcFace on an aligned 112x112 crop)

def _similarity_transform(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Umeyama: 2x3 affine (rotation+uniform scale+translation) mapping src points onto dst."""
    src_mean, dst_mean = src.mean(0), dst.mean(0)
    s, d = src - src_mean, dst - dst_mean
    cov = d.T @ s / len(src)
    U, S, Vt = np.linalg.svd(cov)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, -1] *= -1; R = U @ Vt
    scale = S.sum() / (s ** 2).sum() * len(src)
    t = dst_mean - scale * R @ src_mean
    return np.hstack([scale * R, t[:, None]]).astype(np.float32)


def embed(img: "np.ndarray", kps: np.ndarray) -> np.ndarray:
    from PIL import Image
    _, rec = _sessions()
    M = _similarity_transform(kps.astype(np.float32), TEMPLATE)
    # PIL wants the inverse mapping (output -> input)
    A = np.vstack([M, [0, 0, 1]]); inv = np.linalg.inv(A)
    crop = Image.fromarray(img).transform((112, 112), Image.AFFINE, data=tuple(inv[:2].ravel()), resample=Image.BILINEAR)
    arr = np.asarray(crop).astype(np.float32)[:, :, ::-1]            # BGR
    blob = ((arr - 127.5) / 127.5).transpose(2, 0, 1)[None]
    out = rec.run(None, {rec.get_inputs()[0].name: blob})[0][0]
    return out / (np.linalg.norm(out) + 1e-9)


def face_embedding_from_file(path: Path) -> np.ndarray | None:
    """The main face in a photo (largest), or None when no face is found."""
    from PIL import Image, ImageOps
    img = np.asarray(ImageOps.exif_transpose(Image.open(path)).convert("RGB"))
    faces = [f for f in detect(img) if f["kps"] is not None]
    if not faces:
        return None
    return embed(img, faces[0]["kps"])


# ---------------------------------------------------------------- references (one per person, from the dossier photo)

def rebuild_reference(conn: sqlite3.Connection, pid: int) -> bool:
    from .dossier import photo_paths
    big, _ = photo_paths(pid)
    conn.execute("DELETE FROM face_prints WHERE person_id=?", (pid,))
    if not big.exists() or not available():
        return False
    emb = face_embedding_from_file(big)
    if emb is None:
        return False
    conn.execute("INSERT INTO face_prints(person_id, embedding, source) VALUES(?,?,'photo')", (pid, emb.astype(np.float32).tobytes()))
    return True


def rebuild_missing(conn: sqlite3.Connection, progress=None) -> dict:
    """Reference faces for every person who has a photo but no face print yet (and drop prints of removed photos)."""
    from .dossier import photo_paths
    done = skipped = 0
    rows = db.rows(conn, "SELECT p.id FROM persons p WHERE NOT EXISTS (SELECT 1 FROM face_prints f WHERE f.person_id=p.id)")
    for i, r in enumerate(rows):
        if progress and i % 5 == 0:
            progress("reference faces", i / max(1, len(rows)))
        if not photo_paths(int(r["id"]))[0].exists():
            continue
        with db.tx(conn):
            if rebuild_reference(conn, int(r["id"])):
                done += 1
            else:
                skipped += 1
    return {"built": done, "no_face": skipped}


def references(conn: sqlite3.Connection) -> tuple[np.ndarray, list[int]]:
    rows = db.rows(conn, "SELECT person_id, embedding FROM face_prints")
    if not rows:
        return np.zeros((0, 512), dtype=np.float32), []
    return np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]), [int(r["person_id"]) for r in rows]


def match_frame(conn: sqlite3.Connection, frame: Path, refs=None) -> list[dict]:
    """Faces in one frame matched against the references -> [{person_id, cos, size}] best per face, strongest first."""
    from PIL import Image
    R, pids = refs if refs is not None else references(conn)
    if not len(pids):
        return []
    img = np.asarray(Image.open(frame).convert("RGB"))
    out = []
    for f in detect(img)[:4]:
        if f["kps"] is None:
            continue
        e = embed(img, f["kps"])
        sims = R @ e
        best = {}
        for pid, s in zip(pids, sims):
            if s > best.get(pid, -1):
                best[pid] = float(s)
        pid, s = max(best.items(), key=lambda kv: kv[1])
        if s >= WEAK:
            out.append({"person_id": pid, "cos": round(s, 3), "size": int(f["bbox"][2] - f["bbox"][0])})
    out.sort(key=lambda x: -x["cos"])
    return out


def status(conn: sqlite3.Connection) -> dict:
    return {"models": available(), "references": conn.execute("SELECT COUNT(*) FROM face_prints").fetchone()[0],
            "photos": sum(1 for p in (config.DATA_DIR / "photos").glob("*.jpg") if not p.name.endswith(".thumb.jpg")) if (config.DATA_DIR / "photos").exists() else 0}


# ---------------------------------------------------------------- Wikipedia portraits

def wikipedia_portrait(name: str) -> dict | None:
    """-> {image: bytes, description, title, url} for a person's Wikipedia page, or None when there is no page/photo."""
    import urllib.parse
    import urllib.request
    title = urllib.parse.quote(name.strip().replace(" ", "_"))
    req = urllib.request.Request(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}", headers={"User-Agent": "VoiceFlock/0.2 (local speaker-id app)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read())
    except Exception:
        return None
    if j.get("type") != "standard":
        return None
    img = (j.get("originalimage") or {}).get("source") or (j.get("thumbnail") or {}).get("source")
    if not img:
        return None
    src = img
    if "/thumb/" in src and (j.get("originalimage") or {}).get("width", 0) > 1600:
        src = img   # originalimage already chosen when it exists; thumbnails are fine otherwise
    try:
        with urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "VoiceFlock/0.2 (local speaker-id app)"}), timeout=60) as r:
            data = r.read(25_000_000)
    except Exception:
        return None
    return {"image": data, "description": j.get("description") or "", "title": j.get("title") or name, "url": (j.get("content_urls") or {}).get("desktop", {}).get("page", "")}
