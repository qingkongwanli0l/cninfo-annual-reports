"""按完整历史年度或股票 CSV 下载巨潮年报。"""

import argparse
import csv
import logging
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

from .client import CninfoClient, HttpClient, IdentityError, QueryError, StopBatch, Report, SHANGHAI, select_reports
from .storage import PROJECT, State, run_lock
from .prepare import prepare_inventory


LOG = logging.getLogger(__name__)


def read_targets(path):
    targets = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"stock_code", "report_year"} <= set(reader.fieldnames):
            raise ValueError("CSV 必须包含 stock_code,report_year 两列")
        for line, row in enumerate(reader, 2):
            code = (row["stock_code"] or "").strip()
            year = (row["report_year"] or "").strip()
            if not re.fullmatch(r"\d{6}", code) or not re.fullmatch(r"\d{4}", year):
                raise ValueError(f"CSV 第 {line} 行：需要六位股票代码和四位报告年度")
            target = code, int(year)
            if target not in targets:
                targets.append(target)
    if not targets:
        raise ValueError("CSV 没有下载目标")
    return targets


def query_targets(targets, client, state, versions):
    state.reset_targets()
    for code, year in targets:
        state.target(code, year, "", [], "not_queried")
    groups = {}
    for code in dict.fromkeys(code for code, _ in targets):
        years = {year for input_code, year in targets if input_code == code}
        try:
            company = client.resolve(code)
        except IdentityError as exc:
            for year in years:
                state.target(code, year, "", [], "identity_unresolved", str(exc))
            LOG.error("%s 身份未核实：%s", code, exc)
            continue
        group = groups.setdefault(company["org_id"], {"company": company, "targets": []})
        group["targets"].extend((code, year) for year in sorted(years))

    selected = []
    failures = 0
    for group in groups.values():
        company = group["company"]
        try:
            candidates = client.reports(company, {year for _, year in group["targets"]})
            reports, ambiguities = select_reports(candidates, versions)
        except (QueryError, IdentityError, requests.RequestException, ValueError) as exc:
            failures += 1
            for code, year in group["targets"]:
                state.target(code, year, company["org_id"], [], "query_failed", str(exc))
            LOG.error("%s 查询失败：%s", company["code"], exc)
            if failures >= 3:
                raise StopBatch("连续三家公司查询失败，停止本批次") from exc
            continue
        failures = 0
        for code, year in group["targets"]:
            annual = [report for report in reports if report.report_year == year]
            ambiguous = next((item for item in ambiguities if item["report_year"] == year), None)
            status = "needs_review" if ambiguous else "queried" if annual else "missing"
            state.target(code, year, company["org_id"], annual, status,
                         ambiguous["error"] if ambiguous else "")
        selected.extend(reports)
        LOG.info("%s 查询完成：%d 份完整版本", company["code"], len(reports))
    # 不同代码别名不会占用两次下载名额。
    return list({report.url: report for report in selected}.values())


def query_year(year, data_dir, http, state, versions, refresh=False):
    records, companies, review = prepare_inventory(year, data_dir, http, refresh)
    candidates = [Report(**{key: row[key] for key in Report.__dataclass_fields__}) for row in records]
    selected, ambiguities = select_reports(candidates, versions)
    groups = defaultdict(list)
    for report in selected:
        groups[report.org_id].append(report)
    problems = defaultdict(list)
    for row in review:
        problems[row["org_id"]].append(row["reason"])
    for row in ambiguities:
        problems[row["org_id"]].append(row["error"])
    registrations = []
    seen = set()
    for company in companies:
        code, org = company["stock_code"], company["org_id"]
        reports = groups[org]
        if not reports and org not in problems:
            problems[org].append("没有可选择的未取消完整版本")
        error = "；".join(dict.fromkeys(problems[org]))
        registrations.append((code, year, org, reports, "needs_review" if error else "queried", error))
        seen.add(org)
    for row in review:
        org, code = row["org_id"], row["stock_code"]
        if org not in seen:
            registrations.append((code, year, org, [], "needs_review", "；".join(dict.fromkeys(problems[org]))))
            seen.add(org)
    by_target = {}
    for code, report_year, org, reports, status, error in registrations:
        key = code, report_year
        previous = by_target.get(key)
        if previous is not None:
            error = "；".join(filter(None, [previous[5], error,
                               f"同代码年度存在多个历史公司身份：{previous[2]}、{org}"]))
            reports = list({r.url: r for r in previous[3] + reports}.values())
            org, status = previous[2], "needs_review"
        by_target[key] = (code, report_year, org, reports, status, error)
    state.replace_targets(by_target.values())
    LOG.info("年度 %d：%d 家公司，%d 份完整候选，选择 %d 份，元数据待核实 %d 条，版本歧义 %d 家",
             year, len(companies), len(candidates), len(selected), len(review), len(ambiguities))
    return selected, list(by_target), bool(review or ambiguities)


def fair_order(reports):
    """先覆盖不同公司，再处理各公司的其余版本。"""
    groups = defaultdict(list)
    for report in sorted(reports, key=lambda item: (item.stock_code, item.report_year,
                                                   item.announcement_date, item.announcement_id)):
        groups[report.org_id].append(report)
    ordered = []
    while any(groups.values()):
        for items in groups.values():
            if items:
                ordered.append(items.pop(0))
    return ordered


def download_selected(reports, state, http, limit, pilot=False):
    downloaded = 0
    failed = 0
    pause_pending = False
    budget_reported = False
    for report in fair_order(reports):
        if state.verified(report):
            LOG.info("跳过已校验附件：%s %s %s", report.stock_code, report.report_year,
                     report.announcement_id)
            continue
        if limit is not None and downloaded >= limit:
            continue
        if pilot and not state.reserve_year_pilot(report.url):
            if not budget_reported:
                LOG.info("样本下载累计额度已达 10 份，剩余附件保持待处理")
                budget_reported = True
            continue
        if pause_pending:
            LOG.info("已完成 100 份，休息 60 秒")
            time.sleep(60)
            pause_pending = False
        try:
            state.download(report, http)
        except StopBatch as exc:
            state.update(report.url, "download_failed", str(exc))
            raise
        except (requests.RequestException, QueryError, ValueError) as exc:
            state.update(report.url, "download_failed", str(exc))
            failed += 1
            LOG.error("附件下载失败 %s：%s", report.announcement_id, exc)
            if failed >= 3:
                raise StopBatch("本批次三份附件失败，停止下载并保留结果") from exc
            continue
        downloaded += 1
        LOG.info("已下载 %s %s %s（本次 %d 份）", report.stock_code, report.report_year,
                 report.announcement_id, downloaded)
        if downloaded % 100 == 0:
            state.export()
            pause_pending = True
    return downloaded


def load_document_reviews(data_dir, years):
    reviewed_years, excluded_reports = {}, set()
    for year in sorted(years):
        inventory = data_dir / f"inventory_{year}.sqlite3"
        if not inventory.exists():
            continue
        with closing(sqlite3.connect(inventory.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "year_reviews" in tables:
                reviewed_years.update({(str(aid), str(org)): reviewed_year for aid, org, reviewed_year in
                                       db.execute("SELECT id,org_id,year FROM year_reviews WHERE year=? AND trim(source)<>''",
                                                  (year,))})
            if "excluded_reports" in tables:
                excluded_reports.update((str(aid), str(org)) for aid, org in
                                        db.execute("SELECT id,org_id FROM excluded_reports WHERE year=? AND trim(source)<>''",
                                                   (year,)))
    return reviewed_years, excluded_reports


def run(args, data_dir):
    targets = read_targets(args.input) if args.input is not None else []
    review_pending = False
    with run_lock(data_dir):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            handlers=[logging.StreamHandler(), RotatingFileHandler(
                                data_dir / "run.log", maxBytes=5 * 1024 * 1024,
                                backupCount=3, encoding="utf-8")], force=True)
        state = State(data_dir)
        http = HttpClient()
        interrupted = False
        try:
            state.cleanup_parts()
            if args.year is not None:
                reports, targets, review_pending = query_year(
                    args.year, data_dir, http, state, args.versions, args.refresh)
            else:
                reviewed_years, excluded_reports = load_document_reviews(data_dir, {year for _, year in targets})
                client = CninfoClient(http, data_dir / "stocks.json", reviewed_years=reviewed_years,
                                      excluded_reports=excluded_reports)
                reports = query_targets(targets, client, state, args.versions)
            state.export()
            LOG.info("选择模式 %s，共 %d 份附件；查询预览=%s", args.versions, len(reports), args.dry_run)
            if not args.dry_run:
                download_selected(reports, state, http, args.limit, pilot=args.pilot)
        except (StopBatch, QueryError, requests.RequestException, OSError) as exc:
            LOG.error("运行停止：%s", exc)
            if args.year is not None and not targets:
                LOG.error("年度选择未完成，未执行下载；结果 CSV 保留上次成功选择")
            interrupted = True
        except KeyboardInterrupt:
            LOG.warning("用户中断，已保存完成进度")
            interrupted = True
        finally:
            http.close()
            state.cleanup_parts()
            summaries = state.export()
            pilot_count = state.year_pilot_count()
            state.close()
        current = [row for row in summaries if (row["stock_code"], row["report_year"]) in targets]
        counts = defaultdict(int)
        for row in current:
            counts[row["status"]] += 1
        LOG.info("公司年度状态：%s；样本下载额度 %d/10；清单：%s", dict(counts),
                 pilot_count, data_dir / "results.csv")
        failures = {"query_failed", "download_failed", "identity_unresolved", "needs_review", "not_queried"}
        if interrupted or review_pending or failures.intersection(counts):
            return 1
        if counts.get("missing") or (counts.get("pending") and not args.dry_run):
            return 2
        return 0


def main():
    parser = argparse.ArgumentParser(description="按历史年度或股票 CSV 下载巨潮完整中文年报，默认全量")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="含 stock_code,report_year 的 CSV")
    source.add_argument("--year", type=int, help="历史全市场的报告所属会计年度")
    parser.add_argument("--versions", choices=("all", "latest"), default="all")
    parser.add_argument("--dry-run", action="store_true", help="查询预览，不请求 PDF")
    parser.add_argument("--limit", type=int,
                        help="本次成功新增 PDF 上限，省略时不设上限")
    parser.add_argument("--pilot", action="store_true", help="样本下载，跨重跑累计最多请求10个附件")
    parser.add_argument("--refresh", action="store_true", help="重扫年度历史清单，仅限 --year")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    if args.year is not None and not 2001 <= args.year < datetime.now(SHANGHAI).year:
        parser.error("请指定已经结束的会计年度（2001年起）")
    if args.refresh and args.year is None:
        parser.error("--refresh 仅用于 --year")
    try:
        return run(args, PROJECT / "data")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"错误：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
