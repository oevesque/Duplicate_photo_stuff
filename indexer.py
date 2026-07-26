import hashlib
import os
import threading
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import imagehash
from PIL import Image, ImageFile, ImageOps

ImageFile.LOAD_TRUNCATED_IMAGES = True

from db import Database, ImageRecord, VideoRecord

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif",
    ".gif", ".ico", ".psd", ".tga", ".pcx", ".sgi", ".im",
    ".cr2", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2",
    ".pef", ".srw", ".heic", ".heif", ".avif", ".jp2", ".j2k",
    ".wbmp", ".xbm", ".xpm", ".fpx", ".pcd", ".mcidas",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv", ".3gp",
    ".webm", ".mpg", ".mpeg", ".flv", ".mts", ".m2ts",
}

VIDEO_FRAME_SAMPLES = 5


@dataclass
class IndexProgress:
    current: int = 0
    total: int = 0
    current_file: str = ""
    errors: list = None
    skipped: int = 0

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def is_image(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in IMAGE_EXTENSIONS


def is_video(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in VIDEO_EXTENSIONS


def extract_taken_at(img) -> str:
    try:
        exif = img.getexif()
        # 36867 = DateTimeOriginal (in EXIF IFD), 306 = DateTime (top-level)
        for tag in (36867, 306):
            val = exif.get(tag)
            if val:
                return str(val)
        try:
            exif_ifd = exif.get_ifd(0x8769)
            val = exif_ifd.get(36867)
            if val:
                return str(val)
        except Exception:
            pass
    except Exception:
        pass
    return ""


def compute_hashes(path: str) -> Optional[dict]:
    try:
        with Image.open(path) as img:
            taken_at = extract_taken_at(img)
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            phash = str(imagehash.phash(img, hash_size=16))
            dhash = str(imagehash.dhash(img, hash_size=16))
            phash_90 = str(imagehash.phash(img.rotate(90, expand=True), hash_size=16))
            phash_180 = str(imagehash.phash(img.rotate(180, expand=True), hash_size=16))
            phash_270 = str(imagehash.phash(img.rotate(270, expand=True), hash_size=16))
            width, height = img.size
            fmt = img.format or os.path.splitext(path)[1].lstrip(".").upper()
    except Exception:
        return None

    try:
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        sha256 = sha.hexdigest()
    except Exception:
        sha256 = ""

    return {
        "phash": phash,
        "dhash": dhash,
        "phash_90": phash_90,
        "phash_180": phash_180,
        "phash_270": phash_270,
        "width": width,
        "height": height,
        "format": fmt,
        "sha256": sha256,
        "taken_at": taken_at,
    }


def compute_video_hashes(path: str, num_samples: int = VIDEO_FRAME_SAMPLES) -> Optional[dict]:
    try:
        import cv2
    except ImportError:
        return None

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        cap.release()
        return None

    try:
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = (frame_count / fps) if fps > 0 else 0.0

        if frame_count <= 0:
            cap.release()
            return None

        frame_hashes = []
        # Sample frames evenly, avoiding the very first/last frame (often black/fade).
        for i in range(num_samples):
            frac = (i + 1) / (num_samples + 1)
            target_frame = int(frac * frame_count)
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            frame_hashes.append(str(imagehash.phash(pil_img, hash_size=16)))
    finally:
        cap.release()

    if not frame_hashes:
        return None

    fmt = os.path.splitext(path)[1].lstrip(".").upper()

    try:
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        sha256 = sha.hexdigest()
    except Exception:
        sha256 = ""

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "format": fmt,
        "sha256": sha256,
        "frame_hashes": ",".join(frame_hashes),
    }


def video_similarity_distance(hashes1: list[str], hashes2: list[str]) -> float:
    """Average hamming distance between two lists of sampled frame phashes.
    Assumes both lists were sampled at proportionally equivalent positions."""
    if not hashes1 or not hashes2:
        return 999.0
    n = min(len(hashes1), len(hashes2))
    total = 0
    for i in range(n):
        total += hamming_distance(hashes1[i], hashes2[i])
    return total / n


def compute_color_histogram(path: str, bins: int = 32) -> Optional[np.ndarray]:
    """Compute a normalized RGB color histogram, used to compare images
    that are visually close but not near-identical (different crop/angle)."""
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB").resize((256, 256))
            arr = np.asarray(img)
    except Exception:
        return None
    parts = []
    for c in range(3):
        h, _ = np.histogram(arr[:, :, c], bins=bins, range=(0, 255))
        h = h.astype(np.float64)
        h /= (h.sum() + 1e-9)
        parts.append(h)
    return np.concatenate(parts)


def histogram_similarity(h1: Optional[np.ndarray], h2: Optional[np.ndarray]) -> float:
    """Cosine similarity between two color histograms, in [0, 1]."""
    if h1 is None or h2 is None:
        return 0.0
    denom = (np.linalg.norm(h1) * np.linalg.norm(h2))
    if denom < 1e-9:
        return 0.0
    return float(np.dot(h1, h2) / denom)


def _run_indexing_batch(
    db: Database,
    to_process: list,
    compute_fn: Callable,
    build_record_and_add: Callable,
    progress: IndexProgress,
    progress_callback: Optional[Callable[[IndexProgress], None]],
    pause_event: Optional[threading.Event],
    max_workers: int,
):
    batch_size = max_workers * 2
    commit_every = 100
    since_commit = 0

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        pending = iter(to_process)
        futures = {}

        def submit_next():
            try:
                fp, fsize = next(pending)
                futures[executor.submit(compute_fn, fp)] = (fp, fsize)
                return True
            except StopIteration:
                return False

        for _ in range(batch_size):
            if not submit_next():
                break

        while futures:
            if pause_event is not None:
                pause_event.wait()

            done_set, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED)
            for done in done_set:
                fp, fsize = futures.pop(done)

                if not pause_event or pause_event.is_set():
                    submit_next()

                progress.current_file = fp
                progress.current += 1

                try:
                    info = done.result()
                except Exception:
                    info = None

                if info is None:
                    progress.errors.append(fp)
                else:
                    build_record_and_add(fp, fsize, info)

                since_commit += 1
                if since_commit >= commit_every:
                    db.commit()
                    since_commit = 0

                if progress_callback and progress.current % 10 == 0:
                    progress_callback(progress)

        if since_commit > 0:
            db.commit()


def index_directory(
    db: Database,
    directory: str,
    progress_callback: Optional[Callable[[IndexProgress], None]] = None,
    recursive: bool = True,
    pause_event: Optional[threading.Event] = None,
    max_workers: Optional[int] = None,
    force: bool = False,
) -> IndexProgress:
    directory = os.path.normpath(directory)
    image_files = []
    video_files = []
    if recursive:
        for root, dirs, filenames in os.walk(directory):
            for fn in filenames:
                fp = os.path.join(root, fn)
                if is_image(fp):
                    image_files.append(fp)
                elif is_video(fp):
                    video_files.append(fp)
    else:
        for fn in os.listdir(directory):
            fp = os.path.join(directory, fn)
            if os.path.isfile(fp):
                if is_image(fp):
                    image_files.append(fp)
                elif is_video(fp):
                    video_files.append(fp)

    progress = IndexProgress(total=len(image_files) + len(video_files))

    existing_image_sizes = {} if force else db.get_existing_file_sizes()
    to_process_images = []
    for fp in image_files:
        file_size = os.path.getsize(fp)
        if fp in existing_image_sizes and existing_image_sizes[fp] == file_size:
            progress.skipped += 1
            progress.current += 1
            continue
        to_process_images.append((fp, file_size))

    existing_video_sizes = {} if force else db.get_existing_video_file_sizes()
    to_process_videos = []
    for fp in video_files:
        file_size = os.path.getsize(fp)
        if fp in existing_video_sizes and existing_video_sizes[fp] == file_size:
            progress.skipped += 1
            progress.current += 1
            continue
        to_process_videos.append((fp, file_size))

    if progress_callback:
        progress_callback(progress)

    max_workers = max_workers or min(os.cpu_count() or 4, 8)
    max_workers = min(max_workers, 61)  # Windows ProcessPoolExecutor limit

    print(f"[indexer] Starting: {len(to_process_images)} images, {len(to_process_videos)} videos, {max_workers} workers")

    def add_image_record(fp, fsize, info):
        rec = ImageRecord(
            path=fp,
            filename=os.path.basename(fp),
            directory=os.path.dirname(fp),
            file_size=fsize,
            width=info["width"],
            height=info["height"],
            format=info["format"],
            sha256=info["sha256"],
            phash=info["phash"],
            dhash=info["dhash"],
            phash_90=info["phash_90"],
            phash_180=info["phash_180"],
            phash_270=info["phash_270"],
            taken_at=info["taken_at"],
        )
        db.add_image(rec)

    def add_video_record(fp, fsize, info):
        rec = VideoRecord(
            path=fp,
            filename=os.path.basename(fp),
            directory=os.path.dirname(fp),
            file_size=fsize,
            width=info["width"],
            height=info["height"],
            duration=info["duration"],
            format=info["format"],
            sha256=info["sha256"],
            frame_hashes=info["frame_hashes"],
        )
        db.add_video(rec)

    if to_process_images:
        _run_indexing_batch(
            db, to_process_images, compute_hashes, add_image_record,
            progress, progress_callback, pause_event, max_workers
        )

    if to_process_videos:
        _run_indexing_batch(
            db, to_process_videos, compute_video_hashes, add_video_record,
            progress, progress_callback, pause_event, max_workers
        )

    valid_image_paths = set(image_files)
    removed = db.remove_orphans_in_dir(directory, valid_image_paths, recursive=recursive)
    if removed > 0:
        progress.errors.append(f"{removed} photo(s) supprimee(s) de la base (introuvables sur disque)")

    valid_video_paths = set(video_files)
    removed_v = db.remove_video_orphans_in_dir(directory, valid_video_paths, recursive=recursive)
    if removed_v > 0:
        progress.errors.append(f"{removed_v} video(s) supprimee(s) de la base (introuvables sur disque)")

    db.mark_dir_indexed(directory, recursive=recursive)
    if progress_callback:
        progress_callback(progress)
    return progress


def hamming_distance(hash1: str, hash2: str) -> int:
    if not hash1 or not hash2:
        return 999
    try:
        h1 = imagehash.hex_to_hash(hash1)
        h2 = imagehash.hex_to_hash(hash2)
        return h1 - h2
    except Exception:
        return 999


def hex_to_bool_array(hex_str: str) -> np.ndarray:
    raw = bytes.fromhex(hex_str)
    return np.unpackbits(np.frombuffer(raw, dtype=np.uint8))


def batch_hamming_distances(target_hashes: np.ndarray, other_hashes: np.ndarray) -> np.ndarray:
    """Compute hamming distances between all pairs. Returns (T, N) int array."""
    target_int = target_hashes.astype(np.int32)
    other_int = other_hashes.astype(np.int32)
    target_sums = target_int.sum(axis=1)
    other_sums = other_int.sum(axis=1)
    dot = target_int @ other_int.T
    return target_sums[:, None] + other_sums[None, :] - 2 * dot


def quality_score(rec: ImageRecord) -> float:
    return rec.width * rec.height * (1 + rec.file_size / (rec.width * rec.height + 1) * 0.0001)
