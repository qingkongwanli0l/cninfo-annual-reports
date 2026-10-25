import csv
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests

from cninfo_reports import prepare
from cninfo_reports.client import QUERY_URL, STOCK_URL, SHANGHAI, QueryError


def announcement(number, **changes):
    return {"announcementId": str(number), "secCode": "000001", "orgId": "org1",
            "secName": "示例公司", "announcementTitle": "2021年年度报告",
            "announcementTime": int(datetime(2022, 4, 10, tzinfo=SHANGHAI).timestamp() * 1000),
            "adjunctUrl": f"finalpage/{number}.PDF", "adjunctSize": 123,
            "pageColumn": "SZZB", **changes}


def page(items, total=None, more=False):
    total = len(items) if total is None else total
    return {"announcements": items, "totalRecordNum": total,
            "totalpages": total // 30, "hasMore": more}


class FakeHttp:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def json(self, url, payload=None):
        self.calls.append((url, dict(payload or {})))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result

    def request(self, *args, **kwargs):
        raise AssertionError("名单查询不能请求 PDF 正文")


class PrepareTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "data" / "test-tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(temporary_root.rmdir)
        self.temporary = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.root / "input").mkdir()
        project = patch.object(prepare, "PROJECT", self.root)
        project.start()
        self.addCleanup(project.stop)
        self.path = self.data / "inventory_2021.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        prepare.initialize(self.db, 2021)
        with self.db:
            self.db.execute("DELETE FROM windows")

    def tearDown(self):
        self.db.close()

    def add_window(self, start="2022-01-01", end="2022-12-31", plate=""):
        with self.db:
            prepare.add_window(self.db, "", start, end, plate)

    def read_csv(self, path):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_large_window_splits_without_date_gaps_and_only_queries_metadata(self):
        self.add_window("2022-01-01", "2022-01-04")
        http = FakeHttp([page([], 3001, True), page([announcement(1)]), page([announcement(2)])])
        with self.assertRaisesRegex(QueryError, "拆分窗口总数不一致"):
            prepare.collect(self.db, http)
        self.assertEqual([payload["seDate"] for _, payload in http.calls],
                         ["2022-01-01~2022-01-04", "2022-01-01~2022-01-02", "2022-01-03~2022-01-04"])
        self.assertTrue(all(url == QUERY_URL and payload["stock"] == ""
                            and payload["column"] == "szse" and payload["pageSize"] == 30
                            for url, payload in http.calls))
        self.assertEqual([row[0] for row in self.db.execute("SELECT status FROM windows ORDER BY id")],
                         ["split", "complete", "complete"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 2)
        self.assertEqual(list(self.root.rglob("*.pdf")), [])

    def test_market_split_mismatch_blocks_export_and_explicit_refresh_recovers(self):
        self.add_window("2022-04-30", "2022-04-30")
        prepare.initialize(self.db, 2021)
        with self.db:
            self.db.execute("UPDATE windows SET status='complete',total=0 WHERE category='category_bcgz_szsh'")
        http = FakeHttp([page([], 3001, True), page([announcement(1)]), page([]), page([])])
        with self.assertRaisesRegex(QueryError, "拆分窗口总数不一致"):
            prepare.collect(self.db, http)
        self.assertEqual([payload['plate'] for _, payload in http.calls], ['', 'sh', 'sz', 'bj'])
        self.assertEqual(self.db.execute("SELECT total FROM windows WHERE status='split'").fetchone()[0], 3001)
        self.assertFalse(self.db.execute("SELECT 1 FROM windows WHERE status='pending'").fetchone())
        self.assertEqual(self.db.execute("SELECT value FROM settings WHERE key='discovery_status'").fetchone()[0],
                         'inconsistent')
        previous = self.data / 'reports_2021.csv'
        previous.write_text('previous inventory', encoding='utf-8')
        with self.assertRaisesRegex(QueryError, "拆分窗口总数不一致"):
            prepare.export(self.db, 2021, self.data)
        unchanged = FakeHttp([])
        with self.assertRaisesRegex(QueryError, "refresh"):
            prepare.prepare_inventory(2021, self.data, unchanged)
        self.assertEqual(unchanged.calls, [])
        self.assertEqual(previous.read_text(encoding='utf-8'), 'previous inventory')
        (self.data / 'stocks.json').write_text(json.dumps({'stockList': [], 'fetched_at': time.time()}), encoding='utf-8')

        def response(url, payload=None):
            if url == prepare.GFZR_URL:
                return {'stockList': []}
            self.assertEqual(url, QUERY_URL)
            return page([])

        retry = FakeHttp([])
        retry.json = response
        reports, _, review = prepare.prepare_inventory(2021, self.data, retry, refresh=True)
        self.assertEqual([row['announcement_id'] for row in reports], ['1'])
        self.assertEqual(review, [])
        self.assertFalse(self.db.execute("SELECT 1 FROM windows WHERE status='split'").fetchone())
        self.assertEqual(self.db.execute("SELECT value FROM settings WHERE key='discovery_status'").fetchone()[0],
                         'complete')

    def test_single_day_splits_by_market_and_market_overflow_stays_incomplete(self):
        self.add_window("2022-04-30", "2022-04-30")
        http = FakeHttp([page([], 3001, True), page([]), page([]), page([], 3001, True)])
        with self.assertRaisesRegex(QueryError, "单市场单日"):
            prepare.collect(self.db, http)
        self.assertEqual([payload["plate"] for _, payload in http.calls], ["", "sh", "sz", "bj"])
        self.assertEqual(self.db.execute("SELECT status FROM windows WHERE plate='bj'").fetchone()[0],
                         "pending")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 2)

    def test_page_transaction_rolls_back_and_reopen_resumes_only_unfinished_page(self):
        self.add_window()
        first = [announcement(number) for number in range(1, 31)]
        http = FakeHttp([page(first, 32, True),
                         page([announcement(31), announcement(32, orgId="")], 32)])
        with self.assertRaisesRegex(QueryError, "身份字段"):
            prepare.collect(self.db, http)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 30)
        self.assertEqual(tuple(self.db.execute("SELECT page,fetched,status FROM windows").fetchone()),
                         (2, 30, "pending"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 1)
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        retry = FakeHttp([page([announcement(31), announcement(32)], 32)])
        prepare.collect(self.db, retry)
        self.assertEqual([payload["pageNum"] for _, payload in retry.calls], [2])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 32)
        saved = json.loads(self.db.execute("SELECT record FROM announcements WHERE id='32'").fetchone()[0])
        self.assertEqual(set(saved), set(prepare.FIELDS))
        self.assertEqual(saved["orgId"], "org1")
        prepare.collect(self.db, retry)
        self.assertEqual(len(retry.calls), 1)

    def test_partial_page_overlap_cannot_be_reported_complete(self):
        self.add_window()
        first = [announcement(number) for number in range(1, 31)]
        http = FakeHttp([page(first, 31, True), page([announcement(30)], 31)])
        with self.assertRaises(QueryError):
            prepare.collect(self.db, http)
        self.assertEqual(self.db.execute("SELECT status FROM windows").fetchone()[0], "pending")

    def test_changing_total_requeries_complete_child_ranges(self):
        self.add_window('2022-01-01', '2022-01-02')
        http = FakeHttp([page([announcement(n) for n in range(1, 31)], 31, True),
                         page([announcement(31), announcement(32)], 32),
                         page([announcement(n) for n in range(1, 17)]),
                         page([announcement(n) for n in range(17, 33)])])
        prepare.collect(self.db, http)
        windows = list(self.db.execute('SELECT status,fetched,total FROM windows ORDER BY id'))
        self.assertEqual([tuple(row) for row in windows], [('split', 30, 32), ('complete', 16, 16), ('complete', 16, 16)])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM announcements').fetchone()[0], 32)
        self.assertEqual([payload['seDate'] for _, payload in http.calls][-2:],
                         ['2022-01-01~2022-01-01', '2022-01-02~2022-01-02'])

    def test_market_split_uses_distinct_keys_and_latest_parent_total(self):
        self.add_window('2022-04-30', '2022-04-30')
        http = FakeHttp([page([announcement(n) for n in range(1, 31)], 31, True),
                         page([announcement(31), announcement(32)], 32),
                         page([announcement(n) for n in range(1, 17)]),
                         page([announcement(n) for n in range(17, 32)]), page([announcement(32)])])
        prepare.collect(self.db, http)
        self.assertEqual(self.db.execute("SELECT total FROM windows WHERE status='split'").fetchone()[0], 32)
        self.assertEqual([p['plate'] for _, p in http.calls], ['', '', 'sh', 'sz', 'bj'])
        with self.db:
            self.db.execute("UPDATE window_items SET id='31' WHERE window_id=(SELECT id FROM windows WHERE plate='bj')")
        no_requests = FakeHttp([])
        with self.assertRaisesRegex(QueryError, "拆分窗口总数不一致"):
            prepare.collect(self.db, no_requests)
        self.assertEqual(no_requests.calls, [])

    def test_category_migration_preserves_progress_and_splits_keep_category(self):
        with self.db:
            self.db.execute("DROP TABLE windows")
            self.db.execute('''CREATE TABLE windows(
                id INTEGER PRIMARY KEY,keyword TEXT,start TEXT,end TEXT,plate TEXT,
                page INTEGER DEFAULT 1,fetched INTEGER DEFAULT 0,total INTEGER,
                status TEXT DEFAULT 'pending',UNIQUE(keyword,start,end,plate))''')
            self.db.execute("INSERT INTO windows VALUES (8,'','2022-01-01','2022-01-04','',2,30,30,'complete')")
            self.db.execute("INSERT INTO pages VALUES (8,1,'saved-page')")
        prepare.initialize(self.db, 2021)
        saved = self.db.execute("SELECT * FROM windows WHERE id=8").fetchone()
        self.assertEqual((saved['page'], saved['fetched'], saved['status'], saved['category']),
                         (2, 30, 'complete', 'category_ndbg_szsh'))
        self.assertEqual(tuple(self.db.execute("SELECT * FROM pages").fetchone()), (8, 1, 'saved-page'))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM windows WHERE category='category_bcgz_szsh'").fetchone()[0], 2)
        with self.db:
            self.db.execute("DELETE FROM windows WHERE status='pending'")
            prepare.add_window(self.db, '', '2022-01-01', '2022-01-04', '', 'category_bcgz_szsh')
        http = FakeHttp([page([], 3001, True), page([]), page([])])
        with self.assertRaisesRegex(QueryError, "拆分窗口总数不一致"):
            prepare.collect(self.db, http)
        self.assertTrue(all(payload['category'] == 'category_bcgz_szsh' for _, payload in http.calls))

    def test_classification_retains_historical_org_and_flags_uncertain_records(self):
        stocks = {"000001": {"orgId": "current-org", "category": "A股"},
                  "600001": {"orgId": "non-a", "category": "B股"}}
        cases = [
            (announcement(1, secCode="200001"), "excluded"),
            (announcement(2, secCode="689009"), "excluded"),
            (announcement(3, secCode="600001", orgId="non-a"), "excluded"),
            (announcement(4), "review"),
            (announcement(5, secCode="835999", pageColumn="unknown"), "review"),
            (announcement(6, secCode="600099", orgId="old-org", pageColumn="SHZB"), "report"),
            (announcement(7, secCode="600099", announcementTitle="2021年年度报告（其他附录）"), "review"),
            (announcement(8, secCode="600099", announcementTitle="2020年年度报告"), "excluded"),
            (announcement(9, secCode="600099", adjunctUrl="https://example.com/report.pdf"), "review"),
            (announcement(1212745021, secCode="600531", pageColumn="SHZB",
                          announcementTitle="河南豫光金铅股份有限公司年年度报告"), "review"),
            (announcement(1212708494, secCode="600754", pageColumn="SHZB",
                          announcementTitle="锦江酒店年报"), "review"),
            (announcement(10, secCode="600099", announcementTitle="二〇二一年年度报告"), "review"),
            (announcement(11, secCode="600099", announcementTitle="年度报告摘要"), "excluded"),
            (announcement(1211791981, secCode="600712", pageColumn="SHZB",
                          announcementTitle="南宁百货大楼股份有限公司2016年年度报告（2021年12月更正）"), "excluded"),
        ]
        for item, expected in cases:
            with self.subTest(id=item["announcementId"]):
                kind, reason, report = prepare.classify(item, 2021, stocks)
                self.assertEqual(kind, expected)
                if kind == "report":
                    self.assertEqual(report["org_id"], "old-org")
                else:
                    self.assertTrue(reason)

    def test_complete_export_deduplicates_org_aliases_and_preserves_review(self):
        self.add_window()
        old = announcement(1, secCode="835185", orgId="shared-org", pageColumn="BJS")
        new = announcement(2, secCode="920185", orgId="shared-org", pageColumn="BJS",
                           announcementTime=old["announcementTime"] + 86400000)
        historical = announcement(3, secCode="600099", orgId="historical-org", pageColumn="SHZB")
        unknown = announcement(4, secCode="835999", orgId="unknown-org", pageColumn="unknown")
        items = [old, new, {**old, "secCode": "920185"}, historical, unknown,
                 announcement(5, secCode="200001"), announcement(6, secCode="689009")]
        prepare.collect(self.db, FakeHttp([page(items)]))
        cache = {"stockList": [{"code": code, "orgId": "shared-org", "category": "A股"}
                               for code in ("835185", "920185")]}
        (self.data / "stocks.json").write_text(json.dumps(cache), encoding="utf-8")
        self.assertTrue(prepare.export(self.db, 2021, self.data))
        companies = self.read_csv(self.data / "companies_2021.csv")
        self.assertEqual(len(companies), 2)
        by_org = {row["org_id"]: row for row in companies}
        self.assertEqual(by_org["shared-org"]["stock_code"], "920185")
        self.assertEqual(by_org["shared-org"]["announcement_codes"], "835185|920185")
        self.assertEqual(by_org["shared-org"]["full_versions"], "2")
        self.assertEqual(by_org["historical-org"]["cached_identity"], "historical_announcement_only")
        self.assertEqual(len(self.read_csv(self.data / "reports_2021.csv")), 3)
        self.assertEqual(self.read_csv(self.data / "review_2021.csv")[0]["org_id"], "unknown-org")
        targets = self.read_csv(self.root / "input" / "targets_2021.csv")
        self.assertEqual(targets, [{"stock_code": "600099", "report_year": "2021"},
                                   {"stock_code": "920185", "report_year": "2021"}])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], len(items))
        self.assertEqual(list(self.root.rglob("*.tmp")), [])
        self.assertEqual(list(self.root.rglob("*.pdf")), [])

    def test_incomplete_pagination_does_not_replace_exported_lists(self):
        self.add_window()
        previous = self.data / "companies_2021.csv"
        previous.write_text("previous list", encoding="utf-8")
        target = self.root / "input" / "targets_2021.csv"
        target.write_text("previous targets", encoding="utf-8")
        with self.assertRaisesRegex(QueryError, "查询尚未完成"):
            prepare.export(self.db, 2021, self.data)
        self.assertEqual(previous.read_text(encoding="utf-8"), "previous list")
        self.assertEqual(target.read_text(encoding="utf-8"), "previous targets")
        self.assertFalse((self.data / "reports_2021.csv").exists())

    def test_year_evidence_requires_exact_announcement_org_and_year(self):
        self.add_window()
        title = "河南豫光金铅股份有限公司年年度报告"
        item = announcement(1212745021, secCode="600531", orgId="historical-org",
                            pageColumn="SHZB", announcementTitle=title)
        prepare.collect(self.db, FakeHttp([page([announcement(1), item])]))
        (self.data / "stocks.json").write_text('{"stockList": []}', encoding="utf-8")
        self.assertTrue(prepare.export(self.db, 2021, self.data))
        review = self.read_csv(self.data / "review_2021.csv")
        self.assertEqual([(row["announcement_id"], row["title"]) for row in review],
                         [(item["announcementId"], title)])
        source = "https://www.cninfo.com.cn/new/disclosure/detail?announcementId=1212745021"
        with self.db:
            self.db.executemany('INSERT INTO year_reviews VALUES (?,?,?,?,?)', [
                ("1212745022", item["orgId"], 2021, source, "different announcement"),
                (item["announcementId"], "other-org", 2021, source, "different company"),
                (item["announcementId"], item["orgId"], 2020, source, "different year"),
            ])
        self.assertTrue(prepare.export(self.db, 2021, self.data))
        reports = self.read_csv(self.data / "reports_2021.csv")
        self.assertEqual([row["announcement_id"] for row in reports], ["1"])
        self.assertEqual(reports[0]["year_evidence"], "announcement_title")
        self.assertEqual(self.read_csv(self.data / "review_2021.csv")[0]["announcement_id"],
                         item["announcementId"])
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO year_reviews VALUES (?,?,?,?,?)',
                            (item["announcementId"], item["orgId"], 2021, source, "official year evidence"))
        self.assertFalse(prepare.export(self.db, 2021, self.data))
        reports = {row["announcement_id"]: row for row in self.read_csv(self.data / "reports_2021.csv")}
        verified = reports[item["announcementId"]]
        self.assertEqual((verified["org_id"], verified["report_year"], verified["title"], verified["year_evidence"]),
                         (item["orgId"], "2021", title, source))
        self.assertFalse((self.data / "review_2021.csv").exists())
        saved = json.loads(self.db.execute('SELECT record FROM announcements WHERE id=?',
                                          (item["announcementId"],)).fetchone()[0])
        self.assertEqual(saved["announcementTitle"], title)

    def test_reviewed_summary_exclusion_is_exact_and_keeps_source_record(self):
        self.add_window()
        items = [announcement(1), announcement(2), announcement(3, orgId='org3')]
        prepare.collect(self.db, FakeHttp([page(items)]))
        (self.data / 'stocks.json').write_text('{"stockList": []}', encoding='utf-8')
        with self.db:
            self.db.executemany('INSERT INTO excluded_reports VALUES (?,?,?,?,?)', [
                ('1', 'org1', 2021, 'https://example.com/summary', 'confirmed summary'),
                ('2', 'org1', 2020, 'https://example.com/other', 'different year'),
                ('3', 'other-org', 2021, 'https://example.com/other', 'different company'),
            ])
        self.assertFalse(prepare.export(self.db, 2021, self.data))
        self.assertEqual([row['announcement_id'] for row in self.read_csv(self.data / 'reports_2021.csv')], ['2', '3'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM announcements').fetchone()[0], 3)

    def test_legacy_complete_inventory_reuses_exact_records_without_http(self):
        self.add_window()
        prepare.collect(self.db, FakeHttp([page([announcement(1)])]))
        (self.data / 'stocks.json').write_text('{"stockList": []}', encoding='utf-8')
        with self.db:
            self.db.execute("INSERT INTO settings VALUES ('final_audit','{}')")
            prepare.add_window(self.db, '', '2021-01-01', '2022-12-31', '')
            self.db.execute("UPDATE windows SET status='split',total=3001 WHERE start='2021-01-01'")
        temporary_paths = [self.data / f'{name}_2021.csv.tmp' for name in ('reports', 'companies', 'review')]
        temporary_paths.append(self.root / 'input' / 'targets_2021.csv.tmp')
        for path in temporary_paths:
            path.write_text('interrupted export', encoding='utf-8')
        other_year = self.data / 'reports_2020.csv.tmp'
        other_year.write_text('other year', encoding='utf-8')
        http = FakeHttp([])
        reports, companies, review = prepare.prepare_inventory(2021, self.data, http)
        self.assertEqual([r['announcement_id'] for r in reports], ['1'])
        self.assertEqual(len(companies), 1)
        self.assertEqual(review, [])
        self.assertEqual(http.calls, [])
        self.assertTrue(all(not path.exists() for path in temporary_paths))
        self.assertTrue(other_year.exists())
        self.assertEqual(self.db.execute("SELECT total FROM windows WHERE status='split'").fetchone()[0], 3001)
        self.assertEqual(prepare.load_inventory(self.db, 2021, self.data),
                         (reports, companies, review))

    def test_new_inventory_initializes_cache_and_targets_only_proven_companies(self):
        self.add_window()
        summary = announcement(1213019530, secCode='603637', orgId='zhenhai', pageColumn='SHZB',
                               announcementTitle='镇海石化工程股份有限公司2021年年度报告摘要')
        unusual = announcement(1213019531, secCode='603637', orgId='zhenhai', pageColumn='SHZB',
                               announcementTitle='镇海石化工程股份有限公司2021年年度财务报告全文')
        retired = announcement(50, secCode='400137', orgId='gssh0600146', pageColumn='third')
        mixed_b = announcement(51, secCode='420137', orgId='gssh0600146', pageColumn='third')
        calls = []

        def response(url, payload=None):
            payload = payload or {}
            calls.append((url, dict(payload)))
            if url == STOCK_URL:
                return {'stockList': [{'code': '600146', 'orgId': 'gssh0600146', 'category': 'A股'},
                                      {'code': '200011', 'orgId': 'b-only', 'category': 'B股'}]}
            if url == prepare.GFZR_URL:
                return {'stockList': [
                    {'code': '400137', 'orgId': 'gssh0600146', 'category': '代办转让'},
                    {'code': '420137', 'orgId': 'gssh0600146', 'category': '代办转让'},
                    {'code': '400999', 'orgId': 'neeq-only', 'category': '代办转让'},
                    {'code': '400011', 'orgId': 'b-only', 'category': '代办转让'}]}
            self.assertEqual(url, QUERY_URL)
            if payload['stock'] == '603637,zhenhai':
                self.assertEqual(payload['category'], '')
                self.assertEqual(payload['seDate'], '2022-04-10~2022-04-10')
                return page([summary, unusual])
            if payload['stock']:
                self.assertEqual(payload['column'], 'third')
                self.assertIn(payload['stock'], ('400137,gssh0600146', '420137,gssh0600146'))
                if payload['seDate'] == '2022-01-01~2022-12-31':
                    return page([retired if payload['stock'].startswith('400') else mixed_b])
                return page([])
            if payload['category'] == 'category_ndbg_szsh':
                return page([announcement(1), announcement(2, announcementTitle='2021年年度报告摘要'), summary])
            return page([])

        http = FakeHttp([])
        http.json = response
        reports, companies, review = prepare.prepare_inventory(2021, self.data, http)
        self.assertEqual({r['announcement_id'] for r in reports}, {'1', '50'})
        self.assertEqual({r['org_id'] for r in companies}, {'org1', 'gssh0600146'})
        self.assertTrue(any(r['org_id'] == 'zhenhai' and '摘要' in r['reason'] for r in review))
        self.assertTrue(any(r['stock_code'] == '420137' for r in review))
        self.assertEqual(sum(url == STOCK_URL for url, _ in calls), 1)
        self.assertEqual(sum(url == prepare.GFZR_URL for url, _ in calls), 1)
        self.assertEqual(sum(p.get('stock') == '603637,zhenhai' for _, p in calls), 1)
        self.assertFalse(any(p.get('stock') == '000001,org1' for _, p in calls))
        with self.db:
            self.db.execute('INSERT INTO year_reviews VALUES (?,?,?,?,?)',
                            ('1213019531', 'zhenhai', 2021, 'https://example.com/official-fulltext', 'verified'))
        retry = FakeHttp([])
        reports, _, _ = prepare.prepare_inventory(2021, self.data, retry)
        self.assertIn('1213019531', {r['announcement_id'] for r in reports})
        self.assertEqual(retry.calls, [])
        self.assertEqual(list(self.root.rglob('*.pdf')), [])

    def test_refresh_preserves_records_and_evidence_and_resumes_same_refresh(self):
        self.add_window()
        prepare.collect(self.db, FakeHttp([page([announcement(1), announcement(2)])]))
        (self.data / 'stocks.json').write_text(json.dumps({'stockList': [], 'fetched_at': time.time()}), encoding='utf-8')
        with self.db:
            self.db.execute("INSERT INTO settings VALUES ('final_audit','{}')")
            self.db.execute('INSERT INTO year_reviews VALUES (?,?,?,?,?)',
                            ('1', 'org1', 2021, 'https://example.com/year', 'verified'))
            self.db.execute('INSERT INTO excluded_reports VALUES (?,?,?,?,?)',
                            ('2', 'org1', 2021, 'https://example.com/summary', 'summary'))
        first = [announcement(n) for n in range(100, 130)]
        failing = FakeHttp([page(first, 31, True), requests.ConnectionError('interrupted')])
        with self.assertRaises(requests.ConnectionError):
            prepare.prepare_inventory(2021, self.data, failing, refresh=True)
        interrupted = dict(self.db.execute("SELECT * FROM windows WHERE page=2 AND status='pending'").fetchone())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM announcements WHERE id IN ('1','2')").fetchone()[0], 2)
        calls = []

        def response(url, payload=None):
            payload = payload or {}
            calls.append((url, dict(payload)))
            if url == prepare.GFZR_URL:
                return {'stockList': []}
            self.assertEqual(url, QUERY_URL)
            if len(calls) == 1:
                self.assertEqual(payload['pageNum'], 2)
                self.assertEqual(payload['seDate'], f"{interrupted['start']}~{interrupted['end']}")
                return page([announcement(130)], 31)
            return page([])

        retry = FakeHttp([])
        retry.json = response
        reports, _, review = prepare.prepare_inventory(2021, self.data, retry, refresh=True)
        self.assertEqual(len(reports), 32)
        self.assertEqual(review, [])
        self.assertNotIn('2', {r['announcement_id'] for r in reports})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM year_reviews').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM excluded_reports').fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT value FROM settings WHERE key='discovery_status'").fetchone()[0], 'complete')
        unchanged = FakeHttp([])
        prepare.prepare_inventory(2021, self.data, unchanged)
        self.assertEqual(unchanged.calls, [])

    def test_targeted_windows_keep_stock_and_column_when_split(self):
        with self.db:
            prepare.add_window(self.db, '', '2022-01-01', '2022-01-02', '', '', '400137,org1', 'third')
            prepare.add_window(self.db, '', '2022-01-01', '2022-01-02', '', '', '400148,org2', 'third')
        http = FakeHttp([page([], 3001, True), page([]), page([]), page([])])
        with self.assertRaisesRegex(QueryError, "拆分窗口总数不一致"):
            prepare.collect(self.db, http)
        self.assertEqual({p['stock'] for _, p in http.calls}, {'400137,org1', '400148,org2'})
        self.assertTrue(all(p['column'] == 'third' and p['category'] == '' for _, p in http.calls))
        rows = self.db.execute("SELECT stock,query_column FROM windows WHERE id>2").fetchall()
        self.assertEqual({tuple(row) for row in rows}, {('400137,org1', 'third')})

    def test_annual_windows_clamp_recent_year_and_include_delayed_ranges(self):
        through = '2026-02-01'
        for year in (2020, 2022, 2025):
            with self.subTest(year=year), self.db:
                self.db.execute('DELETE FROM windows')
                prepare.annual_windows(self.db, year, through)
                windows = list(self.db.execute('SELECT keyword,start,end FROM windows'))
                self.assertTrue(all(row['start'] <= row['end'] <= through for row in windows))
                expected = {('', f'{year+1}-01-01', min(f'{year+1}-12-31', through))}
                for keyword in (f'{year}年', str(year)):
                    expected.add((keyword, f'{year}-01-01', f'{year}-12-31'))
                    if year < 2025:
                        expected.add((keyword, f'{year+2}-01-01', through))
                self.assertEqual({tuple(row) for row in windows}, expected)

    def test_review_only_inventory_exports_reasons_and_empty_fulltext_headers(self):
        self.add_window()
        prepare.collect(self.db, FakeHttp([page([announcement(1, announcementTitle='2021年年度报告摘要')])]))
        (self.data / 'stocks.json').write_text('{"stockList": []}', encoding='utf-8')
        reports, companies, review = prepare.load_inventory(self.db, 2021, self.data)
        self.assertEqual((reports, companies), ([], []))
        self.assertEqual(review[0]['org_id'], 'org1')
        self.assertTrue(prepare.export(self.db, 2021, self.data))
        self.assertEqual(self.read_csv(self.data / 'reports_2021.csv'), [])
        self.assertEqual(self.read_csv(self.data / 'companies_2021.csv'), [])
        self.assertEqual(self.read_csv(self.root / 'input' / 'targets_2021.csv'), [])
        with (self.data / 'reports_2021.csv').open(encoding='utf-8-sig', newline='') as handle:
            self.assertIn('announcement_id', csv.DictReader(handle).fieldnames)
        review_path = self.data / 'review_2021.csv'
        saved_review = review_path.read_bytes()
        self.assertEqual(self.read_csv(review_path)[0]['announcement_id'], '1')
        with self.db:
            self.db.execute('DELETE FROM announcements')
        with self.assertRaises(QueryError):
            prepare.export(self.db, 2021, self.data)
        self.assertEqual(review_path.read_bytes(), saved_review)


if __name__ == "__main__":
    unittest.main()
