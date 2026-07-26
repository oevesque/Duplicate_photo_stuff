import sqlite3
import os
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ImageRecord:
    id: Optional[int] = None
    path: str = ""
    filename: str = ""
    directory: str = ""
    file_size: int = 0
    width: int = 0
    height: int = 0
    format: str = ""
    sha256: str = ""
    phash: str = ""
    dhash: str = ""
    phash_90: str = ""
    phash_180: str = ""
    phash_270: str = ""
    taken_at: str = ""
    indexed_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class VideoRecord:
    id: Optional[int] = None
    path: str = ""
    filename: str = ""
    directory: str = ""
    file_size: int = 0
    width: int = 0
    height: int = 0
    duration: float = 0.0
    format: str = ""
    sha256: str = ""
    frame_hashes: str = ""  # comma-separated phash strings, sampled across the video
    indexed_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def frame_hash_list(self) -> list[str]:
        return [h for h in self.frame_hashes.split(",") if h]


SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    directory TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    format TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    phash TEXT NOT NULL,
    dhash TEXT NOT NULL,
    phash_90 TEXT NOT NULL DEFAULT '',
    phash_180 TEXT NOT NULL DEFAULT '',
    phash_270 TEXT NOT NULL DEFAULT '',
    taken_at TEXT NOT NULL DEFAULT '',
    indexed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sha256 ON images(sha256);
CREATE INDEX IF NOT EXISTS idx_directory ON images(directory);
CREATE INDEX IF NOT EXISTS idx_phash ON images(phash);
CREATE INDEX IF NOT EXISTS idx_dhash ON images(dhash);
CREATE TABLE IF NOT EXISTS indexed_dirs (
    dir_path TEXT PRIMARY KEY,
    indexed_at TEXT NOT NULL,
    recursive INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    directory TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    duration REAL NOT NULL DEFAULT 0,
    format TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    frame_hashes TEXT NOT NULL DEFAULT '',
    indexed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_sha256 ON videos(sha256);
CREATE INDEX IF NOT EXISTS idx_video_directory ON videos(directory);
"""


class Database:
    def __init__(self, db_path: str = "doublons.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for stmt in [
            "ALTER TABLE indexed_dirs ADD COLUMN recursive INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE indexed_dirs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE images ADD COLUMN phash_90 TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE images ADD COLUMN phash_180 TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE images ADD COLUMN phash_270 TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE images ADD COLUMN taken_at TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                self.conn.execute(stmt)
            except Exception:
                pass
        self.conn.commit()

    def close(self):
        self.conn.close()

    def add_image(self, rec: ImageRecord) -> int:
        cur = self.conn.execute(
            """INSERT OR REPLACE INTO images
               (path, filename, directory, file_size, width, height, format, sha256, phash, dhash,
                phash_90, phash_180, phash_270, taken_at, indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.path, rec.filename, rec.directory, rec.file_size,
             rec.width, rec.height, rec.format, rec.sha256, rec.phash, rec.dhash,
             rec.phash_90, rec.phash_180, rec.phash_270, rec.taken_at, rec.indexed_at)
        )
        return cur.lastrowid

    def commit(self):
        self.conn.commit()

    def mark_dir_indexed(self, dir_path: str, recursive: bool = True):
        self.conn.execute(
            "INSERT OR REPLACE INTO indexed_dirs (dir_path, indexed_at, recursive, enabled) VALUES (?, ?, ?, ?)",
            (dir_path, datetime.now().isoformat(), 1 if recursive else 0, 1)
        )
        self.conn.commit()

    def get_indexed_dirs(self) -> list[tuple[str, bool, bool]]:
        cur = self.conn.execute("SELECT dir_path, recursive, enabled FROM indexed_dirs ORDER BY dir_path")
        return [(r[0], bool(r[1]), bool(r[2])) for r in cur.fetchall()]

    def set_dir_enabled(self, dir_path: str, enabled: bool):
        self.conn.execute(
            "UPDATE indexed_dirs SET enabled = ? WHERE dir_path = ?",
            (1 if enabled else 0, dir_path)
        )
        self.conn.commit()

    def set_dir_recursive(self, dir_path: str, recursive: bool):
        self.conn.execute(
            "UPDATE indexed_dirs SET recursive = ? WHERE dir_path = ?",
            (1 if recursive else 0, dir_path)
        )
        self.conn.commit()

    def get_enabled_dirs(self) -> list[str]:
        cur = self.conn.execute("SELECT dir_path FROM indexed_dirs WHERE enabled = 1 ORDER BY dir_path")
        return [r[0] for r in cur.fetchall()]

    def get_disabled_dirs(self) -> list[str]:
        cur = self.conn.execute("SELECT dir_path FROM indexed_dirs WHERE enabled = 0 ORDER BY dir_path")
        return [r[0] for r in cur.fetchall()]

    def get_all_directories(self) -> list[str]:
        cur = self.conn.execute("SELECT DISTINCT directory FROM images ORDER BY directory")
        return [r[0] for r in cur.fetchall()]

    def get_directory_counts(self) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT directory, COUNT(*) FROM images GROUP BY directory ORDER BY directory"
        )
        return {r[0]: r[1] for r in cur.fetchall()}

    def get_image_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM images")
        return cur.fetchone()[0]

    def get_existing_file_size(self, path: str) -> Optional[int]:
        cur = self.conn.execute(
            "SELECT file_size FROM images WHERE path = ?", (path,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def get_existing_file_sizes(self) -> dict[str, int]:
        cur = self.conn.execute("SELECT path, file_size FROM images")
        return {r[0]: r[1] for r in cur.fetchall()}

    def get_images_in_directory(self, directory: str, recursive: bool = False) -> list[ImageRecord]:
        if recursive:
            prefix = directory.rstrip(os.sep) + os.sep
            cur = self.conn.execute(
                "SELECT * FROM images WHERE directory = ? OR directory LIKE ? ORDER BY directory, filename",
                (directory, prefix + '%')
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM images WHERE directory = ? ORDER BY filename", (directory,)
            )
        rows = cur.fetchall()
        return [ImageRecord(
            id=r["id"], path=r["path"], filename=r["filename"], directory=r["directory"],
            file_size=r["file_size"], width=r["width"], height=r["height"],
            format=r["format"], sha256=r["sha256"], phash=r["phash"], dhash=r["dhash"],
            phash_90=r["phash_90"], phash_180=r["phash_180"], phash_270=r["phash_270"],
            taken_at=r["taken_at"],
            indexed_at=r["indexed_at"]
        ) for r in rows]

    def get_images_not_in_directory(self, directory: str, recursive: bool = False) -> list[ImageRecord]:
        if recursive:
            prefix = directory.rstrip(os.sep) + os.sep
            cur = self.conn.execute(
                "SELECT * FROM images WHERE directory != ? AND directory NOT LIKE ? ORDER BY directory, filename",
                (directory, prefix + '%')
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM images WHERE directory != ? ORDER BY directory, filename", (directory,)
            )
        rows = cur.fetchall()
        return [ImageRecord(
            id=r["id"], path=r["path"], filename=r["filename"], directory=r["directory"],
            file_size=r["file_size"], width=r["width"], height=r["height"],
            format=r["format"], sha256=r["sha256"], phash=r["phash"], dhash=r["dhash"],
            phash_90=r["phash_90"], phash_180=r["phash_180"], phash_270=r["phash_270"],
            taken_at=r["taken_at"],
            indexed_at=r["indexed_at"]
        ) for r in rows]

    def get_exact_duplicates_outside(self, directory: str, recursive: bool = False) -> dict[str, list[ImageRecord]]:
        if recursive:
            prefix = directory.rstrip(os.sep) + os.sep
            cur = self.conn.execute(
                """SELECT * FROM images WHERE directory != ? AND directory NOT LIKE ? AND sha256 IN (
                    SELECT DISTINCT sha256 FROM images WHERE directory = ? OR directory LIKE ?
                ) ORDER BY sha256, directory, filename""",
                (directory, prefix + '%', directory, prefix + '%')
            )
        else:
            cur = self.conn.execute(
                """SELECT * FROM images WHERE directory != ? AND sha256 IN (
                    SELECT DISTINCT sha256 FROM images WHERE directory = ?
                ) ORDER BY sha256, directory, filename""",
                (directory, directory)
            )
        rows = cur.fetchall()
        result: dict[str, list[ImageRecord]] = {}
        for r in rows:
            rec = ImageRecord(
                id=r["id"], path=r["path"], filename=r["filename"], directory=r["directory"],
                file_size=r["file_size"], width=r["width"], height=r["height"],
                format=r["format"], sha256=r["sha256"], phash=r["phash"], dhash=r["dhash"],
                phash_90=r["phash_90"], phash_180=r["phash_180"], phash_270=r["phash_270"],
                taken_at=r["taken_at"],
                indexed_at=r["indexed_at"]
            )
            result.setdefault(rec.sha256, []).append(rec)
        return result

    def remove_image(self, path: str):
        self.conn.execute("DELETE FROM images WHERE path = ?", (path,))
        self.conn.commit()

    def remove_orphans_in_dir(self, directory: str, valid_paths: set, recursive: bool = False) -> int:
        if recursive:
            prefix = directory.rstrip(os.sep) + os.sep
            cur = self.conn.execute(
                "SELECT path FROM images WHERE directory = ? OR directory LIKE ?",
                (directory, prefix + '%')
            )
        else:
            cur = self.conn.execute(
                "SELECT path FROM images WHERE directory = ?", (directory,)
            )
        orphans = [r[0] for r in cur.fetchall() if r[0] not in valid_paths]
        if orphans:
            self.conn.executemany("DELETE FROM images WHERE path = ?", [(p,) for p in orphans])
            self.conn.commit()
        return len(orphans)

    def remove_images_in_dir(self, dir_path: str):
        self.conn.execute("DELETE FROM images WHERE directory = ?", (dir_path,))
        self.conn.execute("DELETE FROM indexed_dirs WHERE dir_path = ?", (dir_path,))
        self.conn.commit()

    def clear_all(self):
        self.conn.execute("DELETE FROM images")
        self.conn.execute("DELETE FROM videos")
        self.conn.execute("DELETE FROM indexed_dirs")
        self.conn.commit()

    # ---- Videos ----

    def add_video(self, rec: VideoRecord) -> int:
        cur = self.conn.execute(
            """INSERT OR REPLACE INTO videos
               (path, filename, directory, file_size, width, height, duration, format, sha256, frame_hashes, indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.path, rec.filename, rec.directory, rec.file_size,
             rec.width, rec.height, rec.duration, rec.format, rec.sha256, rec.frame_hashes, rec.indexed_at)
        )
        return cur.lastrowid

    def get_existing_video_file_sizes(self) -> dict[str, int]:
        cur = self.conn.execute("SELECT path, file_size FROM videos")
        return {r[0]: r[1] for r in cur.fetchall()}

    def get_video_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM videos")
        return cur.fetchone()[0]

    def get_video_directory_counts(self) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT directory, COUNT(*) FROM videos GROUP BY directory ORDER BY directory"
        )
        return {r[0]: r[1] for r in cur.fetchall()}

    @staticmethod
    def _row_to_video(r) -> VideoRecord:
        return VideoRecord(
            id=r["id"], path=r["path"], filename=r["filename"], directory=r["directory"],
            file_size=r["file_size"], width=r["width"], height=r["height"],
            duration=r["duration"], format=r["format"], sha256=r["sha256"],
            frame_hashes=r["frame_hashes"], indexed_at=r["indexed_at"]
        )

    def get_videos_in_directory(self, directory: str, recursive: bool = False) -> list[VideoRecord]:
        if recursive:
            prefix = directory.rstrip(os.sep) + os.sep
            cur = self.conn.execute(
                "SELECT * FROM videos WHERE directory = ? OR directory LIKE ? ORDER BY directory, filename",
                (directory, prefix + '%')
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM videos WHERE directory = ? ORDER BY filename", (directory,)
            )
        return [self._row_to_video(r) for r in cur.fetchall()]

    def get_videos_not_in_directory(self, directory: str, recursive: bool = False) -> list[VideoRecord]:
        if recursive:
            prefix = directory.rstrip(os.sep) + os.sep
            cur = self.conn.execute(
                "SELECT * FROM videos WHERE directory != ? AND directory NOT LIKE ? ORDER BY directory, filename",
                (directory, prefix + '%')
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM videos WHERE directory != ? ORDER BY directory, filename", (directory,)
            )
        return [self._row_to_video(r) for r in cur.fetchall()]

    def get_exact_video_duplicates_outside(self, directory: str, recursive: bool = False) -> dict[str, list[VideoRecord]]:
        if recursive:
            prefix = directory.rstrip(os.sep) + os.sep
            cur = self.conn.execute(
                """SELECT * FROM videos WHERE directory != ? AND directory NOT LIKE ? AND sha256 IN (
                    SELECT DISTINCT sha256 FROM videos WHERE directory = ? OR directory LIKE ?
                ) ORDER BY sha256, directory, filename""",
                (directory, prefix + '%', directory, prefix + '%')
            )
        else:
            cur = self.conn.execute(
                """SELECT * FROM videos WHERE directory != ? AND sha256 IN (
                    SELECT DISTINCT sha256 FROM videos WHERE directory = ?
                ) ORDER BY sha256, directory, filename""",
                (directory, directory)
            )
        result: dict[str, list[VideoRecord]] = {}
        for r in cur.fetchall():
            rec = self._row_to_video(r)
            result.setdefault(rec.sha256, []).append(rec)
        return result

    def remove_video(self, path: str):
        self.conn.execute("DELETE FROM videos WHERE path = ?", (path,))
        self.conn.commit()

    def remove_video_orphans_in_dir(self, directory: str, valid_paths: set, recursive: bool = False) -> int:
        if recursive:
            prefix = directory.rstrip(os.sep) + os.sep
            cur = self.conn.execute(
                "SELECT path FROM videos WHERE directory = ? OR directory LIKE ?",
                (directory, prefix + '%')
            )
        else:
            cur = self.conn.execute(
                "SELECT path FROM videos WHERE directory = ?", (directory,)
            )
        orphans = [r[0] for r in cur.fetchall() if r[0] not in valid_paths]
        if orphans:
            self.conn.executemany("DELETE FROM videos WHERE path = ?", [(p,) for p in orphans])
            self.conn.commit()
        return len(orphans)
