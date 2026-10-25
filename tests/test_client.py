import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from cninfo_reports.client import (
    CACHE_SECONDS, QUERY_URL, STOCK_URL, TOP_SEARCH_URL,
    CninfoClient, HttpClient, IdentityError, QueryError, Report, StopBatch,
    _clean_title, _report_year, select_reports,
)


COMPANY = {"code": "920185", "org_id": "org1", "name": "示例公司", "aliases": ["835185", "920185"]}


def announcement(number="1", **changes):
    return {"announcementId": number, "announcementTitle": "示例公司2024年年度报告",
            "announcementTime": "2025-04-10", "orgId": "org1", "secCode": "920185",
            "secName": "示例公司", "adjunctUrl": f"finalpage/{number}.PDF", **changes}


def response(status=200, headers=None, body=""):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result._content = body.encode()
    result.close = Mock()
    return result


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name) / "stocks.json"
        self.http = Mock()
        self.client = CninfoClient(self.http, self.cache)

    def test_resolve_exact_and_proven_aliases(self):
        self.http.json.return_value = {"stockList": [
            {"code": "835185", "orgId": "org1", "zwjc": "示例公司"},
            {"code": "920185", "orgId": "org1", "zwjc": "示例公司"},
            {"code": "920999", "orgId": "org2", "zwjc": "其他公司"},
        ]}
        self.assertEqual(self.client.resolve("920185"), COMPANY)
        self.assertEqual(self.client.resolve("835185")["aliases"], COMPANY["aliases"])
        self.http.json.assert_called_once_with(STOCK_URL)
        self.assertFalse(self.cache.with_suffix(".json.tmp").exists())
        cached_client = CninfoClient(self.http, self.cache)
        self.assertEqual(cached_client.resolve("920185"), COMPANY)
        self.assertEqual(self.http.json.call_count, 1)

    def test_missing_code_refreshes_once_without_first_hit_fallback(self):
        with patch("cninfo_reports.client.time.time", return_value=1000000):
            self.cache.write_text(json.dumps({"fetched_at": 1000000, "stockList": []}))
            self.http.json.side_effect = [{"stockList": []}, [{"code": "920999", "orgId": "other"}]]
            with self.assertRaises(IdentityError):
                self.client.resolve("835185")
        self.assertEqual(self.http.json.call_args_list[0].args, (STOCK_URL,))
        self.assertEqual(self.http.json.call_args_list[1].args[0], TOP_SEARCH_URL)

    def test_expired_cache_refreshes_and_exact_search_result_is_saved(self):
        with patch("cninfo_reports.client.time.time", return_value=CACHE_SECONDS + 100):
            self.cache.write_text(json.dumps({"fetched_at": 1, "stockList": []}))
            self.http.json.side_effect = [{"stockList": []}, [{"code": "920185", "orgId": "org1", "zwjc": "示例公司"}]]
            self.assertEqual(self.client.resolve("920185")["org_id"], "org1")
        self.assertEqual(len(json.loads(self.cache.read_text(encoding="utf-8"))["stockList"]), 1)

    def test_invalid_code_never_requests(self):
        with self.assertRaises(IdentityError):
            self.client.resolve("１２３４５６")
        self.http.json.assert_not_called()

    def test_same_company_a_b_shares_do_not_become_aliases(self):
        self.client.stocks = [
            {"code": "000012", "orgId": "gssz0000012", "zwjc": "南玻A", "category": "A股"},
            {"code": "200012", "orgId": "gssz0000012", "zwjc": "南玻B", "category": "B股"},
        ]
        self.assertEqual(self.client.resolve("000012")["aliases"], ["000012"])
        with self.assertRaises(IdentityError):
            self.client.resolve("200012")
        self.http.json.assert_not_called()

    def test_resolve_rejects_non_a_category_and_cdr_code(self):
        for entry in [
            {"code": "689009", "orgId": "cdr", "category": "CDR"},
            {"code": "600001", "orgId": "non-a", "category": "B股"},
            {"code": "900901", "orgId": "b", "category": "B股"},
        ]:
            self.client.stocks = [entry]
            with self.subTest(code=entry["code"]), self.assertRaises(IdentityError):
                self.client.resolve(entry["code"])
        self.http.json.assert_not_called()

    def test_topsearch_cdr_without_category_is_rejected(self):
        self.http.json.side_effect = [{"stockList": []}, [{"code": "689009", "orgId": "cdr"}]]
        with self.assertRaises(IdentityError):
            self.client.resolve("689009")

    def test_same_org_report_cannot_admit_b_shares_or_cdr(self):
        company = {"code": "000012", "org_id": "gssz0000012", "name": "南玻A", "aliases": ["000012", "200012"]}
        self.http.json.return_value = {"announcements": [
            announcement("1", secCode="000012", orgId=company["org_id"]),
            announcement("2", secCode="200012", orgId=company["org_id"]),
            announcement("3", secCode="689009", orgId=company["org_id"]),
        ], "hasMore": False}
        self.assertEqual([item.stock_code for item in self.client.reports(company, {2024})], ["000012"])

    def test_year_fulltext_revisions_and_unrelated_documents(self):
        for title in ["公司2024年年度报告", "2024年度报告全文", "2024年年度报告（修订版）",
                      "2024年年度报告（更正后）", "2024年度报告（更新后）", "2024年报", "2024年年度报告（全文）（修订稿）"]:
            with self.subTest(title=title):
                self.assertEqual(_report_year(title), 2024)
        for title in ["2024年年度报告摘要", "2024年年度报告英文版", "2024年半年度报告",
                      "关于2024年年度报告的更正公告", "2024年年度报告审计报告", "2024年年度报告董事会意见"]:
            with self.subTest(title=title):
                self.assertIsNone(_report_year(title))

    def test_historical_fulltext_title_variants(self):
        for title in ["2021年度年报全文", "600810_2021年_年度报告",
                      "中国交建2021年年度报告_全文", "600232_2021年_年度报告.docx",
                      "中国民生银行2021年年度报告A股",
                      "中信建投证券股份有限公司2021年年度报告（标准版）",
                      "中信建投证券股份有限公司2021年年度报告（可视版）",
                      "2021年年度报告（更新前）", "2021年年度报告（更新后）（更新前）",
                      "华脉科技2021年年度报告（补充版）", "威龙股份2021年年度年度报告",
                      "2021年年度报告（已更新）", "2021年年度报告-修订版", "《中国软件2021年年度报告》",
                      "600805_2021年_年度报告_合并报告", "雅戈尔2021年年度报告（图文版）",
                      "奥康国际：2021年年报报告", "2021年年度报告全文（补充更新版）",
                      "西安曲江文化旅游股份有限公司2021年年度报告（印刷版）",
                      "2021年年度报告（修订稿", "上海海优威新材料股份有限公司2021年年度报告 【更正版】",
                      "广东榕泰：2021年年度报告（二次修订稿）", "2021年年度报告全文（2022年6月订正版）",
                      "广东榕泰：2021年年度报告（三次修订稿）", "2021年年度报告全文-更新",
                      "同达创业2021年年度报告（二次修订版）", "2021年年度报告（更正后）.",
                      "2021年年度报告（四次修订版）", "2021年年度报告全文（补充更正后）",
                      "西菱动力2021年度定期报告全文-更新后"]:
            with self.subTest(title=title):
                self.assertEqual(_report_year(_clean_title(title)), 2021)
        for title in ["2021年年度报告_摘要", "2021年年度报告（英文标准版）",
                      "关于2021年年度报告（更新前）的公告", "2021年度年报审计报告",
                      "2021年年度报告（未知附录）", "2021年年度报告【摘要】"]:
            with self.subTest(title=title):
                self.assertIsNone(_report_year(title))

    def test_title_normalization_preserves_title_and_requires_pdf_attachment(self):
        title = "600232_2021年_年度报告.docx"
        company = {**COMPANY, "code": "600232", "aliases": ["600232"]}
        item = announcement("1212632505", secCode="600232", announcementTitle=title)
        self.http.json.return_value = {"announcements": [item], "hasMore": False}
        reports = self.client.reports(company, {2021})
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].title, title)
        self.assertTrue(reports[0].url.lower().endswith(".pdf"))
        self.http.json.return_value = {"announcements": [{**item, "adjunctUrl": "finalpage/report.docx"}],
                                       "hasMore": False}
        with self.assertRaises(QueryError):
            self.client.reports(company, {2021})

    def test_reviewed_year_uses_exact_identity_and_preserves_pdf_checks(self):
        title = "河南豫光金铅股份有限公司年年度报告"
        item = announcement("1212745021", secCode="600531", announcementTitle=title)
        company = {**COMPANY, "code": "600531", "aliases": ["600531"]}
        client = CninfoClient(self.http, self.cache,
                              reviewed_years={(item["announcementId"], "org1"): 2021,
                                              (item["announcementId"], "other-org"): 2021})
        self.http.json.return_value = {"announcements": [item], "hasMore": False}
        self.assertEqual(self.client.reports(company, {2021}), [])
        reports = client.reports(company, {2021})
        self.assertEqual([(report.report_year, report.title) for report in reports], [(2021, title)])
        for changes in [{"orgId": "other-org"}, {"announcementId": "1212745022"}]:
            self.http.json.return_value = {"announcements": [{**item, **changes}], "hasMore": False}
            with self.subTest(changes=changes):
                self.assertEqual(client.reports(company, {2021}), [])
        self.http.json.return_value = {"announcements": [{**item, "adjunctUrl": "finalpage/report.docx"}],
                                       "hasMore": False}
        with self.assertRaises(QueryError):
            client.reports(company, {2021})

    def test_verified_fulltext_overrides_mislabelled_summary_for_exact_target_only(self):
        item = announcement("1213176074", secCode="600726", orgId="gssh0600726",
                            announcementTitle="华电能源2021年年度报告摘要")
        company = {"code": "600726", "org_id": "gssh0600726", "name": "华电能源", "aliases": ["600726"]}
        self.http.json.return_value = {"announcements": [item], "hasMore": False}
        self.assertEqual(self.client.reports(company, {2021}), [])
        client = CninfoClient(self.http, self.cache, reviewed_years={("1213176074", "gssh0600726"): 2021})
        reports = client.reports(company, {2021})
        self.assertEqual([(report.announcement_id, report.report_year, report.title) for report in reports],
                         [("1213176074", 2021, item["announcementTitle"])])
        for changes in [{"announcementId": "1213176072"}, {"orgId": "other-org"}]:
            self.http.json.return_value = {"announcements": [{**item, **changes}], "hasMore": False}
            with self.subTest(changes=changes):
                self.assertEqual(client.reports(company, {2021}), [])
        self.http.json.return_value = {"announcements": [{**item, "announcementTitle": "2020年年度报告"}],
                                       "hasMore": False}
        self.assertEqual([report.report_year for report in client.reports(company, {2021})], [2021])
        self.assertEqual(client.reports(company, {2020}), [])

    def test_verified_summary_is_excluded_despite_full_report_title(self):
        summary = announcement("1213269898", secCode="600629", orgId="gssh0600629",
                               announcementTitle="华东建筑集团股份有限公司2021年年度报告")
        full = {**summary, "announcementId": "1213269899", "adjunctUrl": "finalpage/full.pdf"}
        company = {"code": "600629", "org_id": "gssh0600629", "name": "华建集团", "aliases": ["600629"]}
        self.http.json.return_value = {"announcements": [summary, full], "hasMore": False}
        client = CninfoClient(self.http, self.cache, excluded_reports={("1213269898", "gssh0600629")})
        self.assertEqual([item.announcement_id for item in client.reports(company, {2021})], ["1213269899"])
        other_org = CninfoClient(self.http, self.cache, excluded_reports={("1213269898", "other-org")})
        self.assertEqual([item.announcement_id for item in other_org.reports(company, {2021})],
                         ["1213269898", "1213269899"])

    def test_reports_pages_identity_market_and_https(self):
        self.http.json.side_effect = [
            {"announcements": [announcement("1", secCode="835185"), announcement("wrong", orgId="org2")], "hasMore": True},
            {"announcements": [announcement("2", announcementTitle="2024年年度报告（修订版）")], "hasMore": False},
        ]
        reports = self.client.reports(COMPANY, {2024})
        self.assertEqual([report.announcement_id for report in reports], ["1", "2"])
        self.assertTrue(all(report.url.startswith("https://static.cninfo.com.cn/") for report in reports))
        first_payload = self.http.json.call_args_list[0].args[1]
        self.assertEqual(first_payload["column"], "szse")
        self.assertEqual(first_payload["plate"], "bj")
        self.assertEqual(first_payload["stock"], "920185,org1")
        self.assertTrue(first_payload["seDate"].startswith("2024-01-01~"))
        self.assertEqual(self.http.json.call_args_list[1].args[1]["pageNum"], 2)

    def test_reports_use_shared_column_and_market_plate(self):
        self.http.json.return_value = {"announcements": [], "totalRecordNum": 0,
                                       "totalpages": 0, "hasMore": False}
        for code, plate in [("600519", "sh"), ("000001", "sz"), ("835185", "bj")]:
            company = {**COMPANY, "code": code, "aliases": [code]}
            with self.subTest(code=code):
                self.assertEqual(self.client.reports(company, {2021}), [])
                payload = self.http.json.call_args.args[1]
                self.assertEqual(payload["column"], "szse")
                self.assertEqual(payload["plate"], plate)

    def test_floor_or_ceil_page_total_keeps_185th_record(self):
        for total_pages in [6, 7]:
            pages = []
            for offset in range(0, 185, 30):
                items = [announcement(str(number)) for number in range(offset, min(offset + 30, 185))]
                pages.append({"announcements": items, "totalRecordNum": 185,
                              "totalpages": total_pages, "hasMore": offset + len(items) < 185})
            self.http.json.reset_mock()
            self.http.json.side_effect = pages
            with self.subTest(totalpages=total_pages):
                reports = self.client.reports(COMPANY, {2024})
                self.assertEqual(len(reports), 185)
                self.assertEqual(reports[-1].announcement_id, "184")
                self.assertEqual(self.http.json.call_count, 7)

    def test_incomplete_query_is_error_and_stop_propagates(self):
        self.http.json.side_effect = [{"announcements": [announcement()], "hasMore": True}, requests.Timeout()]
        with self.assertRaises(QueryError):
            self.client.reports(COMPANY, {2024})
        self.http.json.side_effect = StopBatch("captcha")
        with self.assertRaises(StopBatch):
            self.client.reports(COMPANY, {2024})

    def test_empty_or_repeated_more_page_is_error(self):
        for pages in [
            [{"announcements": [], "hasMore": True}],
            [{"announcements": [announcement()], "hasMore": True}] * 2,
        ]:
            self.http.json.side_effect = pages
            with self.assertRaises(QueryError):
                self.client.reports(COMPANY, {2024})

    def test_unsafe_url_is_never_accepted(self):
        for url in ["https://other.example/report.pdf", "http://static.cninfo.com.cn/report.pdf", "finalpage/notpdf.html"]:
            self.http.json.side_effect = None
            self.http.json.return_value = {"announcements": [announcement(adjunctUrl=url)], "hasMore": False}
            with self.assertRaises(QueryError):
                self.client.reports(COMPANY, {2024})

    def test_invalid_filename_identifiers_fail_query(self):
        for changes in [{"secCode": "../bad"}, {"announcementId": ""}, {"announcementId": "../bad"}]:
            self.http.json.return_value = {"announcements": [announcement(**changes)], "hasMore": False}
            with self.assertRaises(QueryError):
                self.client.reports(COMPANY, {2024})

    def test_conflicting_page_totals_never_return_partial_success(self):
        for totals in [
            {"totalRecordNum": 31, "hasMore": False},
            {"totalRecordNum": 31, "totalpages": 2, "hasMore": True},
            {"totalRecordNum": 1, "totalpages": 2},
            {"totalRecordNum": 0},
        ]:
            self.http.json.return_value = {"announcements": [announcement()], **totals}
            with self.assertRaises(QueryError):
                self.client.reports(COMPANY, {2024})
        self.http.json.return_value = {"announcements": [announcement()], "totalRecordNum": 1, "totalpages": 1, "hasMore": False}
        self.assertEqual(len(self.client.reports(COMPANY, {2024})), 1)

    def test_selection_latest_revision_deduplication_and_ambiguity(self):
        original = Report("org1", "920185", "示例", 2024, "1", "2024年年度报告", "2025-04-01", "https://static.cninfo.com.cn/1.pdf")
        revised = replace(original, announcement_id="2", title="2024年年度报告（修订版）", url="https://static.cninfo.com.cn/2.pdf")
        self.assertEqual(select_reports([original, revised, revised], "latest"), ([revised], []))
        all_reports, errors = select_reports([original, revised, revised], "all")
        self.assertEqual(len(all_reports), 2)
        self.assertEqual(errors, [])
        other_revision = replace(revised, announcement_id="3", url="https://static.cninfo.com.cn/3.pdf")
        selected, errors = select_reports([revised, other_revision], "latest")
        self.assertEqual(selected, [])
        self.assertEqual(errors[0]["report_year"], 2024)
        later = replace(original, announcement_date="2025-05-01")
        self.assertEqual(select_reports([later, revised], "latest"), ([later], []))

    def test_latest_prefers_same_day_supplemental_full_report(self):
        original = Report("org1", "603042", "华脉科技", 2021, "1213086898", "华脉科技2021年年度报告",
                          "2022-04-29", "https://static.cninfo.com.cn/original.pdf")
        supplement = replace(original, announcement_id="1213086899", title="华脉科技2021年年度报告（补充版）",
                             url="https://static.cninfo.com.cn/supplement.pdf")
        self.assertEqual(select_reports([original, supplement], "latest"), ([supplement], []))
        all_reports, errors = select_reports([original, supplement], "all")
        self.assertEqual(set(all_reports), {original, supplement})
        self.assertEqual(errors, [])
        corrected = replace(supplement, title="2021年年度报告全文（2022年6月订正版）")
        self.assertEqual(select_reports([original, corrected], "latest"), ([corrected], []))

    def test_all_retains_withdrawn_full_report_title_but_excludes_summary(self):
        title = "2021年年度报告（已取消）"
        self.http.json.return_value = {
            "announcements": [
                announcement("1212899308", announcementTitle=title),
                announcement("1212899287", announcementTitle="2021年年度报告摘要（已取消）"),
            ],
            "hasMore": False,
        }
        reports = self.client.reports(COMPANY, {2021})
        selected, errors = select_reports(reports, "all")
        self.assertEqual([report.announcement_id for report in selected], ["1212899308"])
        self.assertEqual(selected[0].title, title)
        self.assertEqual(selected[0].report_year, 2021)
        self.assertEqual(errors, [])

    def test_latest_ignores_newer_withdrawn_report_and_withdrawn_only_group(self):
        original = Report("org1", "300733", "西菱动力", 2021, "1212901233",
                          "西菱动力2021年度定期报告全文-更新后", "2022-04-13",
                          "https://static.cninfo.com.cn/original.pdf")
        withdrawn = replace(original, announcement_id="1212899308", title="2021年年度报告（已取消）",
                            announcement_date="2022-04-14", url="https://static.cninfo.com.cn/withdrawn.pdf")
        self.assertEqual(select_reports([original, withdrawn], "latest"), ([original], []))
        self.assertEqual(select_reports([withdrawn], "latest"), ([], []))
        self.assertEqual(select_reports([original, replace(withdrawn, url=original.url)], "latest"),
                         ([original], []))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.sleeps = []
        def sleep(seconds):
            self.sleeps.append(seconds)
            self.clock += seconds
        for name, replacement in [("monotonic", lambda: self.clock), ("sleep", sleep)]:
            patcher = patch(f"cninfo_reports.client.time.{name}", replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("cninfo_reports.client.random.uniform", return_value=6)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.http = HttpClient()
        self.addCleanup(self.http.close)
        self.http.session.request = Mock()

    def test_rate_wait_starts_after_consumption_and_response_closes(self):
        first, second = response(), response()
        self.http.session.request.side_effect = [first, second]
        def consume(result):
            self.clock += 20
            result.close.assert_not_called()
            return 42
        self.assertEqual(self.http.request("GET", QUERY_URL, consume), 42)
        self.http.request("GET", QUERY_URL, lambda result: None)
        self.assertEqual(self.sleeps, [6])
        first.close.assert_called_once()
        second.close.assert_called_once()
        kwargs = self.http.session.request.call_args.kwargs
        self.assertEqual(kwargs["timeout"], (10, 60))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])

    def test_retry_after_and_second_429_stop_batch(self):
        first, second = response(429, {"Retry-After": "40"}), response(429)
        self.http.session.request.side_effect = [first, second]
        with self.assertRaises(StopBatch):
            self.http.request("GET", QUERY_URL, lambda result: None)
        self.assertEqual(self.sleeps, [40])
        self.assertEqual(self.http.session.request.call_count, 2)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_retry_after_from_last_attempt_delays_next_attachment(self):
        limited = response(429, {"Retry-After": "120"})
        self.http.session.request.side_effect = [response(503), response(503), limited, response()]
        consume = Mock(return_value="complete")
        with self.assertRaises(requests.HTTPError):
            self.http.request("GET", "https://static.cninfo.com.cn/first.pdf", consume)
        self.assertEqual(self.http.session.request.call_count, 3)
        self.assertEqual(self.sleeps, [15, 30])
        consume.assert_not_called()
        self.assertEqual(self.http.request("GET", "https://static.cninfo.com.cn/next.pdf", consume),
                         "complete")
        self.assertEqual(self.sleeps, [15, 30, 120])
        self.assertEqual(self.http.session.request.call_count, 4)
        limited.close.assert_called_once()

    def test_gateway_retries_and_callback_errors_are_not_retried(self):
        self.http.session.request.side_effect = [response(503), response(502), response()]
        self.assertEqual(self.http.request("GET", QUERY_URL, lambda result: "ok"), "ok")
        self.assertEqual(self.sleeps, [15, 30])
        self.http.session.request.side_effect = None
        result = response()
        self.http.session.request.return_value = result
        def fail(result):
            raise ValueError("invalid PDF")
        with self.assertRaises(ValueError):
            self.http.request("GET", QUERY_URL, fail)
        self.assertEqual(self.http.session.request.call_count, 4)
        result.close.assert_called_once()

    def test_forbidden_captcha_and_redirect_do_not_retry(self):
        for result, exception in [(response(403), StopBatch),
                                  (response(200, {"Content-Type": "text/html; charset=utf-8"}, "captcha"), StopBatch),
                                  (response(302, {"Location": "https://elsewhere.example"}), requests.HTTPError)]:
            self.http.session.request.reset_mock()
            self.http.session.request.return_value = result
            with self.assertRaises(exception):
                self.http.request("GET", QUERY_URL, lambda result: None)
            self.http.session.request.assert_called_once()
            result.close.assert_called_once()

    def test_stream_interruption_restarts_consumer(self):
        first, second = response(), response()
        self.http.session.request.side_effect = [first, second]
        consume = Mock(side_effect=[requests.exceptions.ChunkedEncodingError(), "complete"])
        self.assertEqual(self.http.request("GET", QUERY_URL, consume), "complete")
        self.assertEqual(consume.call_count, 2)
        self.assertEqual(self.sleeps, [15])
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_retry_rebuilds_range_headers_without_mutating_base_headers(self):
        self.http.session.request.side_effect = [response(), response(206)]
        consume = Mock(side_effect=[requests.ConnectionError("stream interrupted"), "complete"])
        factory = Mock(side_effect=[{}, {"Range": "bytes=4096-", "If-Range": "Thu, 14 Mar 2024 10:43:54 GMT"}])
        headers = {"Accept": "application/pdf"}
        self.assertEqual(self.http.request("GET", QUERY_URL, consume,
                                          headers_factory=factory, headers=headers), "complete")
        first, second = self.http.session.request.call_args_list
        self.assertEqual(first.kwargs["headers"], {"Accept": "application/pdf"})
        self.assertEqual(second.kwargs["headers"]["Range"], "bytes=4096-")
        self.assertEqual(second.kwargs["headers"]["Accept"], "application/pdf")
        self.assertNotIn("headers_factory", second.kwargs)
        self.assertEqual(headers, {"Accept": "application/pdf"})
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(self.sleeps, [15])


if __name__ == "__main__":
    unittest.main()
