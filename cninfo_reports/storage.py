"""附件级状态、PDF 校验及原子归档。"""

import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError
import requests

from .client import StopBatch


LOG = logging.getLogger(__name__)
PROJECT = Path(__file__).resolve().parents[1]


@contextmanager
def run_lock(data_dir):
    """同一项目只运行一个进程，共享串行限制及持久样本额度。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "run.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("本项目已有运行中的进程，请等待其退出") from exc
        yield


def file_hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def validate_pdf(path):
    with path.open("rb") as handle:
        if not handle.read(8).startswith(b"%PDF-"):
            raise ValueError("响应内容不是 PDF")
        handle.seek(max(0, path.stat().st_size - 1024))
        if b"%%EOF" not in handle.read():
            raise ValueError("PDF 缺少文件结束标识，可能未下载完整")
    try:
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise ValueError("PDF 加密，无法验证页面")
        pages = len(reader.pages)
        for page in reader.pages:
            _ = page.mediabox
        if pages < 1:
            raise ValueError("PDF 没有页面")
    except PyPdfError as exc:
        raise ValueError(f"PDF 解析失败：{exc}") from exc
    return pages, path.stat().st_size, file_hash(path)


class State:
    def __init__(self, data_dir):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reports (
                url TEXT PRIMARY KEY, record TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '', pages INTEGER, bytes INTEGER,
                sha256 TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS targets (
                stock_code TEXT, report_year INTEGER, org_id TEXT,
                status TEXT NOT NULL, error TEXT NOT NULL,
                urls TEXT NOT NULL, PRIMARY KEY(stock_code, report_year)
            );
            CREATE TABLE IF NOT EXISTS pilot_requests (url TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS year_pilot_requests (url TEXT PRIMARY KEY);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def reset_targets(self):
        """清单对应本次输入；附件及样本请求历史仍保留。"""
        with self.db:
            self.db.execute("DELETE FROM targets")

    def register(self, report):
        with self.db:
            self.db.execute(
                "INSERT INTO reports(url,record) VALUES (?,?) "
                "ON CONFLICT(url) DO UPDATE SET record=excluded.record",
                (report.url, json.dumps(asdict(report), ensure_ascii=False)),
            )

    def target(self, code, year, org_id, reports, status="queried", error=""):
        for report in reports:
            self.register(report)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO targets VALUES (?,?,?,?,?,?)",
                (code, year, org_id, status, error,
                 json.dumps([report.url for report in reports])),
            )

    def replace_targets(self, targets):
        """年度选择一次事务登记；下载结果仍由 update 逐附件提交。"""
        with self.db:
            self.db.execute("DELETE FROM targets")
            for code, year, org, reports, status, error in targets:
                self.db.executemany(
                    "INSERT INTO reports(url,record) VALUES (?,?) "
                    "ON CONFLICT(url) DO UPDATE SET record=excluded.record",
                    [(report.url, json.dumps(asdict(report), ensure_ascii=False)) for report in reports],
                )
                self.db.execute("INSERT INTO targets VALUES (?,?,?,?,?,?)",
                                (code, year, org, status, error, json.dumps([r.url for r in reports])))

    def row(self, url):
        return self.db.execute("SELECT * FROM reports WHERE url=?", (url,)).fetchone()

    def update(self, url, status, error="", **values):
        values.update(status=status, error=error,
                      updated_at=datetime.now(timezone.utc).isoformat())
        columns = ",".join(f"{key}=?" for key in values)
        with self.db:
            self.db.execute(f"UPDATE reports SET {columns} WHERE url=?",
                            (*values.values(), url))

    def pilot_count(self):
        return self.db.execute("SELECT COUNT(*) FROM pilot_requests").fetchone()[0]

    def reserve_year_pilot(self, url):
        """样本下载跨年份、跨重跑最多请求十个不同附件，失败也占额度。"""
        with self.db:
            if self.db.execute("SELECT 1 FROM year_pilot_requests WHERE url=?", (url,)).fetchone():
                return True
            if self.year_pilot_count() >= 10:
                return False
            self.db.execute("INSERT INTO year_pilot_requests VALUES (?)", (url,))
        return True

    def year_pilot_count(self):
        return self.db.execute("SELECT COUNT(*) FROM year_pilot_requests").fetchone()[0]

    def pdf_path(self, report):
        name = (f"{report.stock_code}_{report.report_year}_"
                f"{report.announcement_date}_{report.announcement_id}.pdf")
        return self.root / "pdf" / report.stock_code / str(report.report_year) / name

    def verified(self, report):
        row = self.row(report.url)
        if row is None or not row["path"]:
            return False
        path = self.root / row["path"]
        if row["status"] == "complete" and path.is_file():
            if path.stat().st_size == row["bytes"] and file_hash(path) == row["sha256"]:
                return True
            self.update(report.url, "pending", "已有文件校验不一致，等待重新下载")
            path.unlink()
        elif row["status"] == "complete":
            self.update(report.url, "pending", "已有文件丢失，等待重新下载")
        # 恢复“文件已原子落盘、状态尚未提交”这一中断窗口。
        if row["status"] == "downloading" and path.is_file():
            try:
                pages, size, digest = validate_pdf(path)
            except ValueError:
                path.unlink()
                self.update(report.url, "pending", "中断后文件校验失败，等待重新下载")
                return False
            self.update(report.url, "complete", pages=pages, bytes=size, sha256=digest)
            return True
        return False

    def cleanup_parts(self):
        pdf_dir = self.root / "pdf"
        if pdf_dir.exists():
            for path in pdf_dir.glob("*/*/*.pdf.part"):
                path.unlink()
        (self.root / "stocks.json.tmp").unlink(missing_ok=True)

    def download(self, report, http):
        final = self.pdf_path(report)
        final.parent.mkdir(parents=True, exist_ok=True)
        part = final.with_suffix(".pdf.part")
        part.unlink(missing_ok=True)
        self.update(report.url, "downloading", path=str(final.relative_to(self.root)))
        validator = None
        expected_size = None

        def headers():
            values = {"Accept-Encoding": "identity"}
            if validator and part.exists() and part.stat().st_size:
                values.update(Range=f"bytes={part.stat().st_size}-", **{"If-Range": validator})
            return values

        def consume(response):
            nonlocal validator, expected_size
            offset = part.stat().st_size if part.exists() else 0
            if response.status_code == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                     response.headers.get("Content-Range", ""))
                if not match or int(match[1]) != offset or int(match[2]) + 1 != int(match[3]):
                    raise ValueError("Range 响应区间与已有附件不一致")
                if not validator or expected_size != int(match[3]):
                    raise ValueError("Range 响应缺少已核对的附件版本或总长度")
                mode = "ab"
            elif response.status_code == 200:
                mode, offset = "wb", 0
                length = response.headers.get("Content-Length")
                expected_size = int(length) if length else None
                etag = response.headers.get("ETag", "")
                validator = (etag if etag and not etag.startswith("W/") else
                             response.headers.get("Last-Modified"))
            else:
                raise ValueError(f"附件响应状态无效：{response.status_code}")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise ValueError("附件响应使用压缩编码，无法安全核对分段长度")
            next_progress = time.monotonic() + 30
            received = offset
            with part.open(mode) as handle:
                first = offset == 0
                for chunk in response.iter_content(chunk_size=4096):
                    if not chunk:
                        continue
                    if first:
                        first = False
                        if not chunk.startswith(b"%PDF-"):
                            text = chunk.decode("utf-8", errors="replace").lower()
                            if any(word in text for word in ("验证码", "captcha", "访问频繁")):
                                raise StopBatch("附件响应要求验证或限制访问，停止批次")
                            raise ValueError("附件响应不是 PDF，未保存为正式文件")
                    handle.write(chunk)
                    received += len(chunk)
                    if time.monotonic() >= next_progress:
                        LOG.info("附件 %s 已接收 %.2f MiB / %s bytes", report.announcement_id,
                                 received / 1048576, expected_size or "未知")
                        next_progress = time.monotonic() + 30
            if expected_size is not None and expected_size != part.stat().st_size:
                raise requests.exceptions.ChunkedEncodingError("附件长度与服务器声明不符")
            return validate_pdf(part)

        try:
            pages, size, digest = http.request("GET", report.url, consume, headers_factory=headers)
            os.replace(part, final)
            self.update(report.url, "complete", pages=pages, bytes=size, sha256=digest)
        finally:
            part.unlink(missing_ok=True)

    def export(self):
        columns = ["input_stock_code", "stock_code", "company_name", "org_id", "report_year",
                   "announcement_id", "title", "announcement_date", "url", "status", "error",
                   "path", "pages", "bytes", "sha256", "same_content_as"]
        known = {row["url"]: dict(row) for row in self.db.execute("SELECT * FROM reports")}
        hashes = {}
        for row in known.values():
            if row["status"] == "complete" and row["sha256"]:
                hashes.setdefault(row["sha256"], []).append(row["url"])
        summaries = []
        with (self.root / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for target in self.db.execute("SELECT * FROM targets ORDER BY stock_code,report_year"):
                urls = json.loads(target["urls"])
                counts = {"complete": 0, "pending": 0, "failed": 0}
                for url in urls:
                    row = known[url]
                    record = json.loads(row["record"])
                    record.update({key: row[key] for key in
                                   ("status", "error", "path", "pages", "bytes", "sha256")})
                    record["input_stock_code"] = target["stock_code"]
                    record["same_content_as"] = " | ".join(
                        other for other in hashes.get(row["sha256"], []) if other != url)
                    writer.writerow(record)
                    group = ("complete" if row["status"] == "complete" else
                             "failed" if row["status"] == "download_failed" else "pending")
                    counts[group] += 1
                status = target["status"]
                if status == "queried":
                    status = ("download_failed" if counts["failed"] else
                              "pending" if counts["pending"] else "complete")
                if not urls:
                    writer.writerow({"input_stock_code": target["stock_code"],
                                     "report_year": target["report_year"], "org_id": target["org_id"],
                                     "status": status, "error": target["error"]})
                summaries.append({"stock_code": target["stock_code"],
                                  "report_year": target["report_year"], "status": status,
                                  **counts, "error": target["error"]})
        with (self.root / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["stock_code", "report_year", "status",
                                                       "complete", "pending", "failed", "error"])
            writer.writeheader()
            writer.writerows(summaries)
        return summaries
