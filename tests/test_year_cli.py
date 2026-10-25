import csv
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from cninfo_reports import __main__ as cli
from cninfo_reports.client import QueryError, Report, StopBatch
from cninfo_reports.storage import State


def report(number, year=2021):
    return Report("org1", "000001", "示例公司", year, f"{number:04}",
                  f"{year}年年度报告", f"{year + 1}-04-10",
                  f"https://static.cninfo.com.cn/test/{year}/{number}.pdf")


class YearDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = State(self.root)
        self.http = Mock()
        self.http.request.side_effect = AssertionError("本测试不能请求网络或 PDF")

    def tearDown(self):
        self.state.close()

    def complete(self, item, http):
        self.assertIs(http, self.http)
        self.state.update(item.url, "complete")

    def download(self, items, limit, pilot=False, effect=None):
        for year in {item.report_year for item in items}:
            self.state.target("000001", year, "org1",
                              [item for item in items if item.report_year == year])
        with patch.object(self.state, "download", side_effect=effect or self.complete) as download, \
                patch.object(self.state, "verified", side_effect=lambda item:
                             self.state.row(item.url)["status"] == "complete"), \
                patch.object(cli.time, "sleep") as sleep:
            count = cli.download_selected(items, self.state, self.http, limit, pilot=pilot)
        self.http.request.assert_not_called()
        self.assertEqual(list(self.root.rglob("*.pdf")), [])
        return count, download, sleep

    def test_nonpilot_limit_ten_skips_completed_without_using_either_pilot_budget(self):
        items = [report(number) for number in range(13)]
        count, download, sleep = self.download(items, 10)
        self.assertEqual((count, download.call_count), (10, 10))
        sleep.assert_not_called()
        self.assertEqual(self.state.year_pilot_count(), 0)
        self.assertEqual(self.state.pilot_count(), 0)
        count, download, _ = self.download(items, 10)
        self.assertEqual((count, download.call_count), (3, 3))
        self.assertEqual(self.state.year_pilot_count(), 0)
        self.assertEqual(self.state.pilot_count(), 0)

    def test_pilot_eight_plus_five_persists_across_years_and_reruns(self):
        first = [report(number) for number in range(8)]
        later = [report(number, 2022) for number in range(5)]
        self.assertEqual(self.download(first, None, pilot=True)[0], 8)
        self.state.close()
        self.state = State(self.root)
        count, download, _ = self.download(first + later, None, pilot=True)
        self.assertEqual((count, download.call_count), (2, 2))
        self.assertEqual(self.state.year_pilot_count(), 10)
        self.assertEqual(self.state.pilot_count(), 0)
        self.assertEqual(self.download(first + later, None, pilot=True)[0], 0)
        summaries = self.state.export()
        self.assertEqual(sum(row["complete"] for row in summaries), 10)
        self.assertEqual(sum(row["pending"] for row in summaries), 3)

    def test_year_pilot_is_independent_of_existing_legacy_pilot_requests(self):
        with self.state.db:
            self.state.db.executemany("INSERT INTO pilot_requests VALUES (?)",
                                      [(f"https://example.invalid/old/{number}",) for number in range(10)])
        self.assertEqual(self.download([report(1)], 10, pilot=True)[0], 1)
        self.assertEqual(self.state.pilot_count(), 10)
        self.assertEqual(self.state.year_pilot_count(), 1)

    def test_failed_attempt_keeps_slot_and_reserved_retry_works_when_budget_full(self):
        retry = report(100)
        count, download, _ = self.download(
            [retry], 10, pilot=True, effect=requests.ConnectionError("interrupted"))
        self.assertEqual((count, download.call_count), (0, 1))
        self.assertEqual(self.state.row(retry.url)["status"], "download_failed")
        self.assertEqual(self.state.year_pilot_count(), 1)
        for number in range(1, 10):
            self.assertTrue(self.state.reserve_year_pilot(report(number).url))
        new = report(0)
        count, download, _ = self.download([new, retry], 10, pilot=True)
        self.assertEqual(count, 1)
        self.assertEqual([call.args[0].url for call in download.call_args_list], [retry.url])
        self.assertEqual(self.state.row(new.url)["status"], "pending")
        self.assertEqual(self.state.year_pilot_count(), 10)

    def test_stop_signal_marks_failure_and_preserves_pilot_slot(self):
        item = report(1)
        with self.assertRaisesRegex(StopBatch, "403"):
            self.download([item], 10, pilot=True, effect=StopBatch("HTTP 403"))
        self.assertEqual(self.state.row(item.url)["status"], "download_failed")
        self.assertEqual(self.state.year_pilot_count(), 1)
        self.http.request.assert_not_called()

    def test_unlimited_exports_and_rests_after_each_hundred_new_files(self):
        items = [report(number) for number in range(201)]
        exported_counts = []
        export = self.state.export

        def capture_export():
            summaries = export()
            exported_counts.append(sum(row["complete"] for row in summaries))
            return summaries

        with patch.object(self.state, "export", side_effect=capture_export):
            count, download, sleep = self.download(items, None)
        self.assertEqual((count, download.call_count), (201, 201))
        self.assertEqual([call.args for call in sleep.call_args_list], [(60,), (60,)])
        self.assertEqual(exported_counts[:2], [100, 200])
        self.assertTrue(all(count in (100, 200, 201) for count in exported_counts))
        self.assertEqual(self.state.year_pilot_count(), 0)
        self.assertEqual(self.state.pilot_count(), 0)
        self.assertEqual(self.download(items, None)[0], 0)

    def test_finite_hundred_does_not_rest_after_reaching_limit(self):
        count, download, sleep = self.download([report(number) for number in range(101)], 100)
        self.assertEqual((count, download.call_count), (100, 100))
        sleep.assert_not_called()

    def test_hundred_new_then_completed_attachment_does_not_rest(self):
        items = [report(number) for number in range(101)]
        self.state.register(items[-1])
        self.state.update(items[-1].url, "complete")
        count, download, sleep = self.download(items, None)
        self.assertEqual((count, download.call_count), (100, 100))
        sleep.assert_not_called()

    def test_query_year_uses_exact_inventory_records_and_keeps_historical_company(self):
        original = replace(report(1), stock_code="400137", org_id="retired-org")
        revised = replace(original, announcement_id="2", title="年报全文",
                          announcement_date="2022-05-10", url=report(2).url)
        withdrawn = replace(original, announcement_id="3", title="2021年年度报告（已取消）",
                            announcement_date="2022-06-10", url=report(3).url)
        records = [dict(asdict(item), year_evidence="https://example.invalid/evidence")
                   for item in (original, revised, withdrawn)]
        companies = [{"stock_code": "400137", "org_id": "retired-org"}]
        with patch.object(cli, "prepare_inventory", return_value=(records, companies, [])) as prepare, \
                patch.object(cli, "CninfoClient") as client:
            selected, targets, pending = cli.query_year(
                2021, self.root, self.http, self.state, "all", refresh=True)
            prepare.assert_called_once_with(2021, self.root, self.http, True)
            self.assertEqual(set(selected), {original, revised, withdrawn})
            self.assertEqual(targets, [("400137", 2021)])
            self.assertFalse(pending)
            self.assertTrue(all(self.state.row(item.url) is not None for item in selected))
            latest, _, pending = cli.query_year(2021, self.root, self.http, self.state, "latest")
            self.assertEqual(latest, [revised])
            self.assertFalse(pending)
            client.assert_not_called()
        self.http.request.assert_not_called()

    def test_query_year_retains_review_only_company_and_latest_ambiguity(self):
        records = [asdict(report(number)) for number in (1, 2)]
        companies = [{"stock_code": "000001", "org_id": "org1"}]
        review = [{"stock_code": "000002", "org_id": "org2", "reason": "报告年度待核实"}]
        with patch.object(cli, "prepare_inventory", return_value=(records, companies, review)):
            selected, targets, pending = cli.query_year(
                2021, self.root, self.http, self.state, "latest")
        self.assertEqual(selected, [])
        self.assertEqual(set(targets), {("000001", 2021), ("000002", 2021)})
        self.assertTrue(pending)
        summaries = self.state.export()
        self.assertEqual({row["status"] for row in summaries}, {"needs_review"})
        self.assertTrue(all(row["error"] for row in summaries))

    def test_year_batch_registration_preserves_old_complete_and_pending_records(self):
        existing = [report(number) for number in range(12)]
        self.state.target("000001", 2021, "org1", existing)
        for number, item in enumerate(existing[:10]):
            self.state.update(item.url, "complete", path=f"pdf/old_{number}.pdf",
                              sha256=f"{number:064x}", pages=100 + number, bytes=1000 + number)
        before = {item.url: dict(self.state.row(item.url)) for item in existing}
        selected_items = [replace(existing[0], company_name="更新简称"), report(12)]
        records = list(map(asdict, selected_items))
        companies = [{"stock_code": "000001", "org_id": "org1"}]
        with patch.object(cli, "prepare_inventory", return_value=(records, companies, [])):
            selected, _, pending = cli.query_year(2021, self.root, self.http, self.state, "all")
        self.assertEqual(set(selected), set(selected_items))
        self.assertFalse(pending)
        for item in existing:
            with self.subTest(url=item.url):
                after = dict(self.state.row(item.url))
                self.assertEqual({key: value for key, value in after.items() if key != "record"},
                                 {key: value for key, value in before[item.url].items() if key != "record"})
        self.assertIn("更新简称", self.state.row(existing[0].url)["record"])
        self.assertEqual(self.state.row(selected_items[1].url)["status"], "pending")
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 13)
        summary = self.state.export()[0]
        self.assertEqual((summary["complete"], summary["pending"]), (1, 1))
        self.http.request.assert_not_called()

    def test_year_dry_run_exports_without_pdf_or_per_company_queries(self):
        args = SimpleNamespace(input=None, year=2021, versions="all", dry_run=True,
                               limit=10, pilot=False, refresh=False)
        data = self.root / "run-data"
        records = [asdict(report(1))]
        companies = [{"stock_code": "000001", "org_id": "org1"}]
        with patch.object(cli, "prepare_inventory", return_value=(records, companies, [])), \
                patch.object(cli, "HttpClient", return_value=self.http), \
                patch.object(cli, "query_targets") as query_targets, \
                patch.object(cli, "download_selected") as download, \
                patch.object(cli.logging, "basicConfig"), \
                patch.object(cli, "RotatingFileHandler"):
            self.assertEqual(cli.run(args, data), 0)
        query_targets.assert_not_called()
        download.assert_not_called()
        self.http.request.assert_not_called()
        with (data / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([(row["announcement_id"], row["status"]) for row in rows],
                         [("0001", "pending")])
        self.assertEqual(list(data.rglob("*.pdf")), [])

    def test_year_run_honors_limit_then_resumes_without_duplicate_downloads(self):
        args = SimpleNamespace(input=None, year=2021, versions="all", dry_run=False,
                               limit=10, pilot=False, refresh=False)
        data = self.root / "run-data"
        items = [report(number) for number in range(13)]
        companies = [{"stock_code": "000001", "org_id": "org1"}]
        with patch.object(cli, "prepare_inventory", return_value=(list(map(asdict, items)), companies, [])), \
                patch.object(cli, "HttpClient", return_value=self.http), \
                patch.object(cli, "query_targets") as query_targets, \
                patch.object(State, "download", autospec=True,
                             side_effect=lambda state, item, http: state.update(item.url, "complete")) as download, \
                patch.object(State, "verified", autospec=True,
                             side_effect=lambda state, item: state.row(item.url)["status"] == "complete"), \
                patch.object(cli.logging, "basicConfig"), \
                patch.object(cli, "RotatingFileHandler"):
            self.assertEqual(cli.run(args, data), 2)
            self.assertEqual(download.call_count, 10)
            self.assertEqual(cli.run(args, data), 0)
            self.assertEqual(download.call_count, 13)
            self.assertEqual(cli.run(args, data), 0)
            self.assertEqual(download.call_count, 13)
        query_targets.assert_not_called()
        self.http.request.assert_not_called()
        with (data / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
            summary = next(csv.DictReader(handle))
        self.assertEqual((summary["status"], summary["complete"]), ("complete", "13"))

    def test_year_run_downloads_confirmed_report_and_retains_same_company_review(self):
        args = SimpleNamespace(input=None, year=2021, versions="all", dry_run=False,
                               limit=None, pilot=False, refresh=False)
        data = self.root / "run-data"
        item = report(1)
        companies = [{"stock_code": "000001", "org_id": "org1"}]
        review = [{"stock_code": "000001", "org_id": "org1", "reason": "另一个标题待核实"}]
        with patch.object(cli, "prepare_inventory", return_value=([asdict(item)], companies, review)), \
                patch.object(cli, "HttpClient", return_value=self.http), \
                patch.object(State, "download", autospec=True,
                             side_effect=lambda state, item, http: state.update(item.url, "complete")) as download, \
                patch.object(cli.logging, "basicConfig"), \
                patch.object(cli, "RotatingFileHandler"):
            self.assertEqual(cli.run(args, data), 1)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(download.call_args.args[1], item)
        self.http.request.assert_not_called()
        with (data / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
            summary = next(csv.DictReader(handle))
        self.assertEqual((summary["status"], summary["complete"], summary["pending"]),
                         ("needs_review", "1", "0"))
        self.assertEqual(summary["error"], review[0]["reason"])
        with (data / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
            result = next(csv.DictReader(handle))
        self.assertEqual((result["announcement_id"], result["status"]), ("0001", "complete"))

    def test_same_code_historical_org_review_keeps_confirmed_download_and_one_target(self):
        item = replace(report(1), org_id="current-org")
        companies = [{"stock_code": "000001", "org_id": "current-org"}]
        review = [{"stock_code": "000001", "org_id": "history-org", "reason": "历史身份待核实"}]
        with patch.object(cli, "prepare_inventory", return_value=([asdict(item)], companies, review)):
            selected, targets, pending = cli.query_year(2021, self.root, self.http, self.state, "all")
        self.assertEqual(selected, [item])
        self.assertEqual(targets, [("000001", 2021)])
        self.assertTrue(pending)
        with patch.object(self.state, "download", side_effect=self.complete) as download:
            self.assertEqual(cli.download_selected(selected, self.state, self.http, None), 1)
        download.assert_called_once_with(item, self.http)
        summaries = self.state.export()
        self.assertEqual(len(summaries), 1)
        self.assertEqual((summaries[0]["status"], summaries[0]["complete"]), ("needs_review", 1))
        self.assertIn("current-org", summaries[0]["error"])
        self.assertIn("history-org", summaries[0]["error"])
        self.http.request.assert_not_called()

    def test_year_prepare_failure_preserves_old_selection_and_stops_before_download(self):
        items = [report(1), report(2)]
        self.state.target("000001", 2021, "org1", items)
        self.state.update(items[0].url, "complete", path="pdf/old.pdf", sha256="a" * 64,
                          pages=100, bytes=1000)
        targets_before = [dict(row) for row in self.state.db.execute("SELECT * FROM targets")]
        reports_before = [dict(row) for row in self.state.db.execute("SELECT * FROM reports ORDER BY url")]
        args = SimpleNamespace(input=None, year=2021, versions="all", dry_run=False,
                               limit=None, pilot=False, refresh=False)
        with patch.object(cli, "prepare_inventory", side_effect=QueryError("查询未完整完成")), \
                patch.object(cli, "HttpClient", return_value=self.http), \
                patch.object(cli, "download_selected") as download, \
                patch.object(cli.logging, "basicConfig"), \
                patch.object(cli, "RotatingFileHandler"):
            self.assertEqual(cli.run(args, self.root), 1)
        download.assert_not_called()
        self.assertEqual([dict(row) for row in self.state.db.execute("SELECT * FROM targets")], targets_before)
        self.assertEqual([dict(row) for row in self.state.db.execute("SELECT * FROM reports ORDER BY url")],
                         reports_before)
        self.http.request.assert_not_called()

    def test_main_both_sources_default_to_unlimited_all_without_pilot(self):
        for source, value in (("--year", "2021"), ("--input", "not-opened.csv")):
            with self.subTest(source=source):
                with patch.object(sys, "argv", ["cninfo_reports", source, value]), \
                        patch.object(cli, "run", return_value=0) as run:
                    self.assertEqual(cli.main(), 0)
                args, data = run.call_args.args
                self.assertEqual((args.year, args.input),
                                 (2021, None) if source == "--year" else (None, Path(value)))
                self.assertEqual(args.versions, "all")
                self.assertIsNone(args.limit)
                self.assertFalse(args.pilot)
                self.assertFalse(args.refresh)
                self.assertEqual(data, cli.PROJECT / "data")

    def test_main_passes_explicit_pilot_limit_refresh_and_dry_run(self):
        with patch.object(sys, "argv", ["cninfo_reports", "--year", "2021", "--pilot",
                                        "--limit", "5", "--refresh", "--dry-run"]), \
                patch.object(cli, "run", return_value=0) as run:
            self.assertEqual(cli.main(), 0)
        args = run.call_args.args[0]
        self.assertEqual(args.limit, 5)
        self.assertTrue(args.pilot)
        self.assertTrue(args.refresh)
        self.assertTrue(args.dry_run)


if __name__ == "__main__":
    unittest.main()
