import csv
import hashlib
import io
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from pypdf import PdfWriter

from cninfo_reports import __main__ as cli
from cninfo_reports.client import Report, select_reports
from cninfo_reports.storage import State


def report(number, code="000001", org_id="org1", year=2024):
    return Report(org_id, code, "示例公司", year, str(number), f"{year}年年度报告",
                  "2025-04-10", f"https://static.cninfo.com.cn/{number}.pdf")


def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeHttp:
    def __init__(self, bodies=None):
        self.pdf = pdf_bytes()
        self.bodies = bodies or {}
        self.urls = []
        self.headers = []

    def request(self, method, url, consume, headers_factory=None):
        self.urls.append(url)
        self.headers.append(headers_factory() if headers_factory else {})
        body = self.bodies.get(url, self.pdf)
        response = SimpleNamespace(status_code=200, headers={"Content-Length": str(len(body))},
                                   iter_content=lambda chunk_size: iter([body]))
        return consume(response)

    def close(self):
        pass


class InterruptedHttp(FakeHttp):
    def __init__(self, wrong_range=False):
        super().__init__()
        self.split = len(self.pdf) // 2
        self.modified = "Wed, 21 Oct 2026 07:28:00 GMT"
        self.wrong_range = wrong_range

    def request(self, method, url, consume, headers_factory=None):
        def interrupted_chunks(chunk_size):
            yield self.pdf[:self.split]
            raise requests.ConnectionError("CDN interrupted after a partial body")

        self.urls.append(url)
        self.headers.append(headers_factory() if headers_factory else {})
        first = SimpleNamespace(status_code=200,
                                headers={"Accept-Ranges": "bytes", "Last-Modified": self.modified,
                                         "Content-Length": str(len(self.pdf))},
                                iter_content=interrupted_chunks)
        try:
            return consume(first)
        except requests.ConnectionError:
            self.urls.append(url)
            self.headers.append(headers_factory() if headers_factory else {})
            offset = self.split + int(self.wrong_range)
            tail = self.pdf[self.split:]
            second = SimpleNamespace(status_code=206,
                                     headers={"Accept-Ranges": "bytes", "Last-Modified": self.modified,
                                              "Content-Length": str(len(tail)),
                                              "Content-Range": f"bytes {offset}-{len(self.pdf) - 1}/{len(self.pdf)}"},
                                     iter_content=lambda chunk_size: iter([tail]))
            return consume(second)


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "data" / "test-tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(temporary_root.rmdir)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = State(self.root / "data")
        self.http = FakeHttp()

    def tearDown(self):
        self.state.close()

    def test_pilot_budget_persists_across_runs_and_reruns(self):
        first = [report(f"{number:02}") for number in range(8)]
        later = [report(f"{number:02}") for number in range(8, 13)]
        self.state.target("000001", 2024, "org1", first)
        self.assertEqual(cli.download_selected(first, self.state, self.http, 10, pilot=True), 8)
        self.state.close()
        self.state = State(self.root / "data")
        self.state.target("000001", 2024, "org1", first + later)
        self.assertEqual(cli.download_selected(first + later, self.state, self.http, 10, pilot=True), 2)
        self.assertEqual(self.state.year_pilot_count(), 10)
        self.assertEqual(len(list(self.state.root.rglob("*.pdf"))), 10)
        self.assertEqual(cli.download_selected(first + later, self.state, self.http, 10, pilot=True), 0)
        self.assertEqual(len(self.http.urls), 10)
        summary = self.state.export()[0]
        self.assertEqual((summary["status"], summary["complete"], summary["pending"]),
                         ("pending", 10, 3))

    def test_all_versions_with_identical_content_have_distinct_files(self):
        original = report("1")
        revised = replace(report("2"), title="2024年年度报告（修订版）",
                          announcement_date="2025-05-10")
        selected, errors = select_reports([original, revised, original], "all")
        self.assertEqual(errors, [])
        self.state.target("000001", 2024, "org1", selected)
        self.assertEqual(cli.download_selected(selected, self.state, self.http, 10, pilot=True), 2)
        rows = [self.state.row(item.url) for item in selected]
        self.assertNotEqual(rows[0]["path"], rows[1]["path"])
        self.assertEqual(rows[0]["sha256"], rows[1]["sha256"])
        self.assertTrue(all(self.state.verified(item) for item in selected))
        self.state.export()
        with (self.state.root / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
            results = list(csv.DictReader(handle))
        self.assertEqual({row["same_content_as"] for row in results},
                         {original.url, revised.url})
        self.assertEqual(select_reports([original, revised], "latest"), ([revised], []))

    def test_alias_targets_share_one_query_and_download(self):
        shared = report("1", "835185", "org1")
        other = report("2", "920999", "org2")
        client = Mock()
        client.resolve.side_effect = lambda code: {
            "code": code, "org_id": "org2" if code == "920999" else "org1",
        }
        client.reports.side_effect = lambda company, years: [
            other if company["org_id"] == "org2" else shared]
        targets = [("835185", 2024), ("920185", 2024), ("920999", 2024)]
        selected = cli.query_targets(targets, client, self.state, "all")
        self.assertEqual(client.reports.call_count, 2)
        self.assertEqual(len(selected), 2)
        self.assertEqual(cli.download_selected(selected, self.state, self.http, 10, pilot=True), 2)
        self.assertEqual(len(self.http.urls), 2)
        summaries = self.state.export()
        self.assertEqual(len(summaries), 3)
        self.assertTrue(all(row["status"] == "complete" for row in summaries))

    def test_atomic_file_recovers_after_database_update_interrupted(self):
        item = report("1")
        self.state.target("000001", 2024, "org1", [item])
        self.state.reserve_year_pilot(item.url)
        update = self.state.update

        def interrupted_update(url, status, *args, **values):
            if status == "complete":
                raise RuntimeError("simulate interruption after rename")
            return update(url, status, *args, **values)

        with patch.object(self.state, "update", side_effect=interrupted_update):
            with self.assertRaisesRegex(RuntimeError, "after rename"):
                self.state.download(item, self.http)
        self.assertEqual(self.state.row(item.url)["status"], "downloading")
        self.assertTrue(self.state.pdf_path(item).exists())
        self.assertEqual(list(self.state.root.rglob("*.part")), [])
        self.state.close()
        self.state = State(self.root / "data")
        self.assertEqual(cli.download_selected([item], self.state, self.http, 10, pilot=True), 0)
        self.assertEqual(len(self.http.urls), 1)
        self.assertEqual(self.state.row(item.url)["status"], "complete")
        self.assertEqual(self.state.year_pilot_count(), 1)

    def test_partial_stream_resumes_with_range_and_if_range(self):
        item = report("1")
        self.state.target("000001", 2024, "org1", [item])
        http = InterruptedHttp()
        self.assertEqual(cli.download_selected([item], self.state, http, 10, pilot=True), 1)
        self.assertNotIn("Range", http.headers[0])
        self.assertEqual(http.headers[1]["Range"], f"bytes={http.split}-")
        self.assertEqual(http.headers[1]["If-Range"], http.modified)
        self.assertEqual(self.state.pdf_path(item).read_bytes(), http.pdf)
        self.assertEqual(self.state.row(item.url)["sha256"], hashlib.sha256(http.pdf).hexdigest())
        self.assertEqual(self.state.row(item.url)["status"], "complete")
        self.assertEqual(self.state.year_pilot_count(), 1)
        self.assertEqual(list(self.state.root.rglob("*.part")), [])

    def test_wrong_content_range_is_not_archived(self):
        item = report("1")
        self.state.target("000001", 2024, "org1", [item])
        http = InterruptedHttp(wrong_range=True)
        self.assertEqual(cli.download_selected([item], self.state, http, 10, pilot=True), 0)
        self.assertEqual(http.headers[1]["Range"], f"bytes={http.split}-")
        self.assertEqual(self.state.row(item.url)["status"], "download_failed")
        self.assertFalse(self.state.pdf_path(item).exists())
        self.assertEqual(list(self.state.root.rglob("*.part")), [])

    def test_changed_hash_is_not_skipped_and_stale_part_is_removed(self):
        item = report("1")
        self.state.target("000001", 2024, "org1", [item])
        cli.download_selected([item], self.state, self.http, 10, pilot=True)
        path = self.state.pdf_path(item)
        body = bytearray(path.read_bytes())
        body[-1] ^= 1
        path.write_bytes(body)
        self.assertFalse(self.state.verified(item))
        part = path.with_suffix(".pdf.part")
        part.write_bytes(b"partial")
        self.state.cleanup_parts()
        self.assertFalse(part.exists())
        self.assertEqual(cli.download_selected([item], self.state, self.http, 10, pilot=True), 1)
        self.assertEqual(path.read_bytes(), self.http.pdf)
        self.assertEqual(self.state.year_pilot_count(), 1)

    def test_limit_does_not_leave_corrupt_file_marked_complete(self):
        existing = report("3")
        self.state.target("000001", 2024, "org1", [existing])
        cli.download_selected([existing], self.state, self.http, 10, pilot=True)
        self.state.pdf_path(existing).write_bytes(b"damaged")
        new = report("1")
        pending = report("2")
        items = [new, pending, existing]
        self.state.target("000001", 2024, "org1", items)
        self.assertEqual(cli.download_selected(items, self.state, self.http, 1, pilot=True), 1)
        summary = self.state.export()[0]
        self.assertEqual(summary["complete"], 1)
        self.assertEqual(summary["status"], "pending")

    def test_failed_attachment_leaves_no_pdf_and_keeps_pilot_reservation(self):
        item = report("1")
        self.state.target("000001", 2024, "org1", [item])
        self.http.bodies[item.url] = b"<html>not a PDF</html>"
        self.assertEqual(cli.download_selected([item], self.state, self.http, 10, pilot=True), 0)
        self.assertEqual(self.state.row(item.url)["status"], "download_failed")
        self.assertEqual(self.state.year_pilot_count(), 1)
        self.assertFalse(self.state.pdf_path(item).exists())
        self.assertEqual(list(self.state.root.rglob("*.part")), [])
        self.http.bodies.clear()
        self.assertEqual(cli.download_selected([item], self.state, self.http, 10, pilot=True), 1)
        self.assertEqual(self.state.year_pilot_count(), 1)

    def test_new_attachment_does_not_block_reserved_retry_when_pilot_full(self):
        reserved = [report(str(number)) for number in range(90, 100)]
        for item in reserved:
            self.state.reserve_year_pilot(item.url)
        new = report("00")
        retry = reserved[-1]
        self.state.target("000001", 2024, "org1", [new, retry])
        self.state.update(retry.url, "download_failed", "previous connection failed")
        self.assertEqual(cli.download_selected([new, retry], self.state, self.http, 10, pilot=True), 1)
        self.assertEqual(self.http.urls, [retry.url])
        self.assertEqual(self.state.year_pilot_count(), 10)
        self.assertEqual(self.state.row(new.url)["status"], "pending")

    def test_cli_mixed_status_summary_and_exit_codes(self):
        success = report("1")
        failure = report("3", "000003", "org3")
        client = Mock()
        client.resolve.side_effect = lambda code: {"code": code, "org_id": f"org{int(code)}"}
        client.reports.side_effect = lambda company, years: {
            "000001": [success], "000002": [], "000003": [failure],
        }[company["code"]]
        http = FakeHttp({failure.url: b"not PDF"})
        source = self.root / "targets.csv"
        source.write_text("stock_code,report_year\n000001,2024\n000002,2024\n000003,2024\n",
                          encoding="utf-8")
        args = SimpleNamespace(input=source, year=None, pilot=True, refresh=False, versions="all", dry_run=False, limit=10)
        data_dir = self.root / "run-data"
        with patch.object(cli, "HttpClient", return_value=http), \
                patch.object(cli, "CninfoClient", return_value=client), \
                patch.object(cli.logging, "basicConfig"), \
                patch.object(cli, "RotatingFileHandler"):
            self.assertEqual(cli.run(args, data_dir), 1)
            with (data_dir / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
                summaries = {row["stock_code"]: row for row in csv.DictReader(handle)}
            self.assertEqual({code: row["status"] for code, row in summaries.items()},
                             {"000001": "complete", "000002": "missing", "000003": "download_failed"})
            http.bodies.clear()
            self.assertEqual(cli.run(args, data_dir), 2)
            self.assertEqual(http.urls.count(success.url), 1)
            source.write_text("stock_code,report_year\n000001,2024\n000003,2024\n", encoding="utf-8")
            self.assertEqual(cli.run(args, data_dir), 0)

    def test_cli_loads_only_sourced_reviews_for_target_years_and_accepts_legacy_database(self):
        data_dir = self.root / "run-data"
        data_dir.mkdir()
        inventory = sqlite3.connect(data_dir / "inventory_2021.sqlite3")
        try:
            with inventory:
                inventory.execute('CREATE TABLE year_reviews(id TEXT,org_id TEXT,year INTEGER,source TEXT,note TEXT)')
                inventory.executemany('INSERT INTO year_reviews VALUES (?,?,?,?,?)', [
                    ("1212745021", "org1", 2021, "https://www.cninfo.com.cn/verified", "verified"),
                    ("2", "org1", 2021, "", "unverified"),
                    ("3", "org1", 2021, "   ", "unverified"),
                    ("4", "org1", 2021, None, "unverified"),
                    ("5", "org1", 2022, "https://www.cninfo.com.cn/other", "other year"),
                ])
                inventory.execute('CREATE TABLE excluded_reports(id TEXT,org_id TEXT,year INTEGER,source TEXT,note TEXT)')
                inventory.executemany('INSERT INTO excluded_reports VALUES (?,?,?,?,?)', [
                    ("1213269898", "gssh0600629", 2021, "https://www.sse.com.cn/verified", "verified summary"),
                    ("6", "org1", 2021, "", "unverified"),
                    ("7", "org1", 2022, "https://www.sse.com.cn/other", "other year"),
                ])
        finally:
            inventory.close()
        sqlite3.connect(data_dir / "inventory_2022.sqlite3").close()
        (data_dir / "inventory_2024.sqlite3").write_bytes(b"unrequested inventory must not be opened")
        source = self.root / "targets.csv"
        source.write_text("stock_code,report_year\n000001,2021\n000001,2022\n000001,2023\n", encoding="utf-8")
        args = SimpleNamespace(input=source, year=None, pilot=True, refresh=False, versions="all", dry_run=True, limit=10)
        client = Mock()
        client.resolve.return_value = {"code": "000001", "org_id": "org1"}
        client.reports.return_value = [report(str(year), year=year) for year in (2021, 2022, 2023)]
        with patch.object(cli, "HttpClient", return_value=self.http), \
                patch.object(cli, "CninfoClient", return_value=client) as constructor, \
                patch.object(cli.logging, "basicConfig"), \
                patch.object(cli, "RotatingFileHandler"):
            self.assertEqual(cli.run(args, data_dir), 0)
            constructor.assert_called_once_with(self.http, data_dir / "stocks.json",
                                                reviewed_years={("1212745021", "org1"): 2021},
                                                excluded_reports={("1213269898", "gssh0600629")})
        self.assertFalse((data_dir / "inventory_2023.sqlite3").exists())
        self.assertEqual(self.http.urls, [])


if __name__ == "__main__":
    unittest.main()
