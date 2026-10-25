"""Prepare a historical annual-report inventory without requesting PDF bodies."""

import argparse
import csv
import hashlib
import json
import logging
import re
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

from .storage import PROJECT, run_lock
from .client import (CACHE_SECONDS, CninfoClient, HttpClient, PAGE_SIZE, QUERY_URL, SHANGHAI, QueryError, StopBatch,
                     _clean_title, _is_a_share, _pdf_url, _report_year,
                     page_has_more)

LOG = logging.getLogger(__name__)
FIELDS = ('secCode', 'secName', 'orgId', 'announcementId', 'announcementTitle',
          'announcementTime', 'adjunctUrl', 'adjunctSize', 'pageColumn')
BOARDS = {'SHZB', 'SHKCB', 'SZZB', 'SZCY', 'BJS'}
GFZR_URL = 'https://www.cninfo.com.cn/new/data/gfzr_stock.json'


def initialize(db, year):
    windows_sql = '''
        CREATE TABLE IF NOT EXISTS windows(
            id INTEGER PRIMARY KEY,keyword TEXT,start TEXT,end TEXT,plate TEXT,
            page INTEGER DEFAULT 1,fetched INTEGER DEFAULT 0,total INTEGER,
            status TEXT DEFAULT 'pending',category TEXT DEFAULT 'category_ndbg_szsh',
            stock TEXT DEFAULT '',query_column TEXT DEFAULT 'szse',
            UNIQUE(category,keyword,start,end,plate,stock,query_column))'''
    db.execute(windows_sql)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS pages(
            window_id INTEGER,page INTEGER,fingerprint TEXT,
            PRIMARY KEY(window_id,page),UNIQUE(window_id,fingerprint));
        CREATE TABLE IF NOT EXISTS announcements(
            id TEXT,code TEXT,org_id TEXT,record TEXT,
            PRIMARY KEY(id,code,org_id));
        CREATE TABLE IF NOT EXISTS window_items(
            window_id INTEGER,id TEXT,code TEXT,org_id TEXT,
            PRIMARY KEY(window_id,id,code,org_id));
        CREATE TABLE IF NOT EXISTS year_reviews(
            id TEXT,org_id TEXT,year INTEGER,source TEXT,note TEXT,
            PRIMARY KEY(id,org_id));
        CREATE TABLE IF NOT EXISTS excluded_reports(
            id TEXT,org_id TEXT,year INTEGER,source TEXT,note TEXT,
            PRIMARY KEY(id,org_id));
    ''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(windows)')}
    if not {'category', 'stock', 'query_column'} <= columns:
        with db:
            db.execute('ALTER TABLE windows RENAME TO windows_old')
            db.execute(windows_sql)
            copied = ','.join(name for name in ('id', 'keyword', 'start', 'end', 'plate', 'page', 'fetched',
                                                'total', 'status', 'category', 'stock', 'query_column') if name in columns)
            db.execute(f'INSERT INTO windows({copied}) SELECT {copied} FROM windows_old')
            db.execute('DROP TABLE windows_old')
    saved = db.execute("SELECT value FROM settings WHERE key='through'").fetchone()
    through = saved[0] if saved else datetime.now(SHANGHAI).date().isoformat()
    legacy_complete = (db.execute("SELECT 1 FROM settings WHERE key='final_audit'").fetchone()
                       and not db.execute("SELECT 1 FROM settings WHERE key='discovery_status'").fetchone()
                       and not db.execute("SELECT 1 FROM windows WHERE status='pending'").fetchone())
    with db:
        if not saved:
            db.execute('INSERT INTO settings VALUES (?,?)', ('through', through))
            db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('created_at', datetime.now(SHANGHAI).isoformat()))
            annual_windows(db, year, through)
        if not legacy_complete:
            for keyword in (f'{year}年', str(year)):
                add_window(db, keyword, f'{year}-01-01', through, '', 'category_bcgz_szsh')


def add_window(db, keyword, start, end, plate, category='category_ndbg_szsh', stock='', column='szse'):
    db.execute('INSERT OR IGNORE INTO windows(keyword,start,end,plate,category,stock,query_column) VALUES (?,?,?,?,?,?,?)',
               (keyword, start, end, plate, category, stock, column))


def annual_windows(db, year, through, stock='', column='szse', category='category_ndbg_szsh'):
    add_window(db, '', f'{year+1}-01-01', min(f'{year+1}-12-31', through), '', category, stock, column)
    for keyword in (f'{year}年', str(year)):
        add_window(db, keyword, f'{year}-01-01', f'{year}-12-31', '', category, stock, column)
        if f'{year+2}-01-01' <= through:
            add_window(db, keyword, f'{year+2}-01-01', through, '', category, stock, column)


def split_window(db, window, total):
    start, end = date.fromisoformat(window['start']), date.fromisoformat(window['end'])
    with db:
        if start < end:
            middle = start + (end - start) // 2
            add_window(db, window['keyword'], start.isoformat(), middle.isoformat(), window['plate'],
                       window['category'], window['stock'], window['query_column'])
            add_window(db, window['keyword'], (middle + timedelta(days=1)).isoformat(), end.isoformat(), window['plate'],
                       window['category'], window['stock'], window['query_column'])
        elif not window['plate'] and not window['stock'] and window['query_column'] == 'szse':
            for plate in ('sh', 'sz', 'bj'):
                add_window(db, window['keyword'], window['start'], window['end'], plate, window['category'])
        else:
            raise QueryError('单市场单日超过100页，不能宣称该范围完整')
        db.execute("UPDATE windows SET status='split',total=? WHERE id=?", (total, window['id']))


def collect(db, http):
    while True:
        window = db.execute("SELECT * FROM windows WHERE status='pending' ORDER BY start,id LIMIT 1").fetchone()
        if window is None:
            for parent in db.execute("SELECT * FROM windows WHERE status='split' AND total IS NOT NULL"):
                count = db.execute('''SELECT COUNT(*) FROM (
                    SELECT DISTINCT item.id,item.code,item.org_id
                    FROM window_items item JOIN windows child ON child.id=item.window_id
                    WHERE child.status='complete' AND child.category=? AND child.keyword=?
                      AND child.stock=? AND child.query_column=? AND child.start>=? AND child.end<=?
                      AND (?='' OR child.plate=?))''',
                    (parent['category'], parent['keyword'], parent['stock'], parent['query_column'],
                     parent['start'], parent['end'], parent['plate'], parent['plate'])).fetchone()[0]
                if count != parent['total']:
                    with db:
                        db.execute("INSERT OR REPLACE INTO settings VALUES ('discovery_status','inconsistent')")
                    raise QueryError(f"拆分窗口总数不一致：{parent['start']}~{parent['end']} "
                                     f"父窗口{parent['total']}条，子窗口去重后{count}条；请用--refresh重新查询")
            return
        payload = {'pageNum': window['page'], 'pageSize': PAGE_SIZE, 'tabName': 'fulltext',
                   'column': window['query_column'], 'plate': window['plate'], 'stock': window['stock'],
                   'category': window['category'], 'searchkey': window['keyword'],
                   'isHLtitle': 'false', 'seDate': f"{window['start']}~{window['end']}"}
        response = http.json(QUERY_URL, payload)
        if not isinstance(response, dict) or 'announcements' not in response:
            raise QueryError('名单查询缺少公告列表')
        items = response['announcements'] or []
        total = response.get('totalRecordNum', response.get('totalAnnouncement'))
        if not isinstance(items, list) or not isinstance(total, int) or total < 0:
            raise QueryError('名单查询总数或列表格式无效')
        if window['page'] == 1 and total > PAGE_SIZE * 100:
            LOG.info('拆分日期范围 %s~%s：%d条', window['start'], window['end'], total)
            split_window(db, window, total)
            continue
        if window['total'] is not None and total != window['total']:
            LOG.warning('窗口总数变化 %s~%s（原%d，本页%d），拆分后完整重查子窗口',
                        window['start'], window['end'], window['total'], total)
            split_window(db, window, total)
            continue
        fetched = window['fetched'] + len(items)
        more = page_has_more(response, window['page'], fetched, len(items))
        keys = [(str(x.get('announcementId')), str(x.get('secCode')), str(x.get('orgId'))) for x in items]
        fingerprint = hashlib.sha256(json.dumps(keys).encode()).hexdigest()
        if db.execute('SELECT 1 FROM pages WHERE window_id=? AND fingerprint=?',
                      (window['id'], fingerprint)).fetchone():
            raise QueryError('接口重复返回同一页，名单未完成')
        with db:
            for item in items:
                aid, code, org = (str(item.get(k) or '') for k in ('announcementId', 'secCode', 'orgId'))
                if not re.fullmatch(r'[0-9]+', aid) or not re.fullmatch(r'[0-9]{6}', code) or not org:
                    raise QueryError('公告身份字段不完整，名单未完成')
                if window['stock'] and org != window['stock'].split(',', 1)[1]:
                    raise QueryError('定向补查返回了其他公司的公告，名单未完成')
                if db.execute('SELECT 1 FROM window_items WHERE window_id=? AND id=? AND code=? AND org_id=?',
                              (window['id'], aid, code, org)).fetchone():
                    raise QueryError('同一窗口分页包含重复公告，名单未完成')
                db.execute('INSERT INTO window_items VALUES (?,?,?,?)', (window['id'], aid, code, org))
                record = {key: item.get(key) for key in FIELDS}
                db.execute('INSERT OR REPLACE INTO announcements VALUES (?,?,?,?)',
                           (aid, code, org, json.dumps(record, ensure_ascii=False)))
            db.execute('INSERT INTO pages VALUES (?,?,?)', (window['id'], window['page'], fingerprint))
            db.execute('UPDATE windows SET page=?,fetched=?,total=?,status=? WHERE id=?',
                       (window['page'] + 1, fetched, total, 'pending' if more else 'complete', window['id']))
        pages = db.execute('SELECT COUNT(*) FROM pages').fetchone()[0]
        LOG.info('清单已保存%d页；%s %s~%s 关键词=%r 第%d页 %d/%d条',
                 pages, window['category'], window['start'], window['end'], window['keyword'], window['page'], fetched, total)


def classify(item, year, stocks, year_evidence=None, retired_pairs=()):
    code, org = str(item['secCode']), str(item['orgId'])
    title = _clean_title(item['announcementTitle'] or '')
    current = stocks.get(code)
    retired = (code, org) in retired_pairs
    if code.startswith(('400', '420')) and not retired:
        return 'excluded', '转让代码缺少历史A股身份关联', None
    if not retired and not _is_a_share(code, current.get('category') if current else None):
        return 'excluded', '非A股代码或证券类别', None
    if current and str(current['orgId']) != org:
        return 'review', '历史代码与当前映射orgId不一致', None
    if not retired and item.get('pageColumn') not in BOARDS and not (current and current.get('category') == 'A股'):
        return 'review', '公告市场身份待核实', None
    title_year = _report_year(title)
    if title_year != year and not year_evidence:
        suspect = (title_year is None and (str(year) in title or not re.search(r'20[0-9]{2}', title))
                   and re.search(r'年度报告|年报', title)
                   and not re.search(r'摘要|英文|英语|English|半年度|半年报|关于|公告|意见|说明|审计|董事会|监事会|取消', title, re.I))
        return ('review', '全年报告标题后缀待核实', None) if suspect else ('excluded', '非目标年度完整中文年报', None)
    if retired and code.startswith('420'):
        return 'review', '420转让证券类别待核实，不作为A股别名', None
    try:
        url = _pdf_url(item['adjunctUrl'])
        published = datetime.fromtimestamp(item['announcementTime'] / 1000, SHANGHAI).date().isoformat()
    except (TypeError, ValueError):
        return 'review', '附件地址或公告日期待核实', None
    return 'report', '', {'stock_code': code, 'company_name': item['secName'], 'org_id': org,
                         'report_year': year, 'announcement_id': str(item['announcementId']),
                         'title': title, 'announcement_date': published, 'page_column': item['pageColumn'],
                         'url': url, 'size_kib': item['adjunctSize'],
                         'withdrawn': 'yes' if '已取消' in title else 'no',
                         'year_evidence': year_evidence or 'announcement_title'}


def write_csv(path, fields, rows):
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def historical_a_orgs(db, stocks):
    orgs = {str(row['orgId']) for row in stocks.values()
            if _is_a_share(str(row['code']), row.get('category')) and row.get('category') == 'A股'}
    for row in db.execute('SELECT record FROM announcements'):
        item = json.loads(row[0])
        code, org = str(item['secCode']), str(item['orgId'])
        current = stocks.get(code)
        if (not code.startswith(('400', '420')) and item.get('pageColumn') in BOARDS
                and _is_a_share(code, current.get('category') if current else None)
                and (not current or str(current['orgId']) == org)):
            orgs.add(org)
    return orgs


def retired_pairs(db, stocks):
    saved = db.execute("SELECT value FROM settings WHERE key='retired_mapping'").fetchone()
    orgs = historical_a_orgs(db, stocks)
    return {(str(row['code']), str(row['orgId'])) for row in json.loads(saved[0]) if
            re.fullmatch(r'(?:400|420)[0-9]{3}', str(row['code'])) and str(row['orgId']) in orgs
            and row.get('category') == '代办转让'} if saved else set()


def summary_only(db, year, stocks, retired, full_orgs):
    candidates = {}
    for row in db.execute('SELECT record FROM announcements ORDER BY id,code'):
        item = json.loads(row[0])
        title = _clean_title(item['announcementTitle'] or '')
        if str(item['orgId']) in full_orgs or '摘要' not in title:
            continue
        kind, _, report = classify(dict(item, announcementTitle=title.replace('摘要', '')),
                                   year, stocks, retired_pairs=retired)
        if kind == 'report':
            candidates[(report['org_id'], report['stock_code'], report['announcement_date'])] = item
    return candidates


def load_inventory(db, year, root):
    if db.execute("SELECT 1 FROM settings WHERE key='discovery_status' AND value='inconsistent'").fetchone():
        raise QueryError('拆分窗口总数不一致，不输出全量目标清单；请用--refresh重新查询')
    if db.execute("SELECT 1 FROM windows WHERE status='pending'").fetchone():
        raise QueryError('查询尚未完成，不输出全量目标清单')
    cache = json.loads((root / 'stocks.json').read_text(encoding='utf-8'))
    stocks = {str(row['code']): row for row in cache['stockList']}
    retired = retired_pairs(db, stocks)
    verified = {(row['id'], row['org_id']): row['source'] for row in
                db.execute("SELECT * FROM year_reviews WHERE year=? AND trim(source)<>''", (year,))}
    excluded = {tuple(row) for row in
                db.execute("SELECT id,org_id FROM excluded_reports WHERE year=? AND trim(source)<>''", (year,))}
    reports, review, seen_ids, seen_urls = [], [], set(), set()
    for row in db.execute('SELECT record FROM announcements ORDER BY id,code'):
        item = json.loads(row[0])
        if (str(item['announcementId']), str(item['orgId'])) in excluded:
            continue
        kind, reason, report = classify(item, year, stocks,
                                       verified.get((str(item['announcementId']), str(item['orgId']))), retired)
        if kind == 'report':
            if report['announcement_id'] in seen_ids or report['url'] in seen_urls:
                continue
            reports.append(report)
            seen_ids.add(report['announcement_id']); seen_urls.add(report['url'])
        elif kind == 'review':
            review.append({'stock_code': item['secCode'], 'org_id': item['orgId'],
                           'announcement_id': item['announcementId'], 'title': item['announcementTitle'], 'reason': reason})
    groups = defaultdict(list)
    for report in reports:
        groups[report['org_id']].append(report)
    reviewed_orgs = {str(row['org_id']) for row in review}
    for (org, code, _), item in summary_only(db, year, stocks, retired, groups).items():
        if org not in reviewed_orgs:
            review.append({'stock_code': code, 'org_id': org, 'announcement_id': str(item['announcementId']),
                           'title': item['announcementTitle'], 'reason': '仅有目标年度摘要，未识别到完整年报'})
            reviewed_orgs.add(org)
    companies = []
    for org, group in groups.items():
        latest = max(group, key=lambda r: (r['announcement_date'], r['announcement_id']))
        code = latest['stock_code']
        companies.append({'stock_code': code, 'report_year': year, 'company_name': latest['company_name'],
                          'org_id': org, 'announcement_codes': '|'.join(sorted({r['stock_code'] for r in group})),
                          'full_versions': len(group), 'latest_date': latest['announcement_date'],
                          'cached_identity': 'matched' if code in stocks and str(stocks[code]['orgId']) == org else 'historical_announcement_only'})
    companies.sort(key=lambda r: r['stock_code'])
    reports.sort(key=lambda r: (r['stock_code'], r['announcement_date'], r['announcement_id']))
    return reports, companies, review


def export(db, year, root, inventory=None):
    reports, companies, review = inventory if inventory is not None else load_inventory(db, year, root)
    if not companies and not review:
        raise QueryError('未发现可导出的公司，不覆盖已有清单')
    report_fields = list(reports[0]) if reports else [
        'stock_code', 'company_name', 'org_id', 'report_year', 'announcement_id', 'title',
        'announcement_date', 'page_column', 'url', 'size_kib', 'withdrawn', 'year_evidence']
    company_fields = list(companies[0]) if companies else [
        'stock_code', 'report_year', 'company_name', 'org_id', 'announcement_codes',
        'full_versions', 'latest_date', 'cached_identity']
    write_csv(root / f'reports_{year}.csv', report_fields, reports)
    write_csv(root / f'companies_{year}.csv', company_fields, companies)
    review_path = root / f'review_{year}.csv'
    if review:
        write_csv(review_path, list(review[0]), review)
    else:
        review_path.unlink(missing_ok=True)
    target_path = root.parent / 'input' / f'targets_{year}.csv'
    target_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(target_path, ['stock_code', 'report_year'],
              [{k: row[k] for k in ('stock_code', 'report_year')} for row in companies])
    LOG.info('公司%d家，完整版本%d份，待核实%d条；目标清单 %s', len(companies), len(reports), len(review), target_path)
    return bool(review)


def ensure_stock_cache(root, http):
    path = root / 'stocks.json'
    try:
        cache = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        cache = {}
    if not (isinstance(cache, dict) and isinstance(cache.get('stockList'), list)
            and isinstance(cache.get('fetched_at'), (int, float))
            and 0 <= time.time() - cache['fetched_at'] < CACHE_SECONDS):
        CninfoClient(http, path)._refresh()


def schedule_supplements(db, year, root, http):
    cache = json.loads((root / 'stocks.json').read_text(encoding='utf-8'))
    stocks = {str(row['code']): row for row in cache['stockList']}
    data = http.json(GFZR_URL)
    if not isinstance(data, dict) or not isinstance(data.get('stockList'), list):
        raise QueryError('退市转让字典缺少stockList')
    saved = db.execute("SELECT value FROM settings WHERE key='retired_mapping'").fetchone()
    orgs = historical_a_orgs(db, stocks)
    mappings = {(str(row['code']), str(row['orgId'])): row
                for row in (json.loads(saved[0]) if saved else []) + data['stockList']
                if re.fullmatch(r'(?:400|420)[0-9]{3}', str(row.get('code', '')))
                and str(row.get('orgId')) in orgs and row.get('category') == '代办转让'}
    reports, _, _ = load_inventory(db, year, root)
    through = db.execute("SELECT value FROM settings WHERE key='through'").fetchone()[0]
    with db:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                   ('retired_mapping', json.dumps(list(mappings.values()), ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('retired_mapping_source', GFZR_URL))
        for code, org in sorted(mappings):
            annual_windows(db, year, through, f'{code},{org}', 'third', '')
        for (org, code, published), _ in summary_only(db, year, stocks, mappings,
                                                    {row['org_id'] for row in reports}).items():
            add_window(db, '', published, published, '', '', f'{code},{org}')
        db.execute("INSERT OR REPLACE INTO settings VALUES ('supplements_scheduled','yes')")


def prepare_inventory(year, root, http, refresh=False):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for name in ('reports', 'companies', 'review'):
        (root / f'{name}_{year}.csv.tmp').unlink(missing_ok=True)
    (root.parent / 'input' / f'targets_{year}.csv.tmp').unlink(missing_ok=True)
    db = sqlite3.connect(root / f'inventory_{year}.sqlite3')
    db.row_factory = sqlite3.Row
    try:
        initialize(db, year)
        settings = dict(db.execute('SELECT key,value FROM settings'))
        complete = (settings.get('discovery_status') == 'complete'
                    or ('discovery_status' not in settings and 'final_audit' in settings
                        and not db.execute("SELECT 1 FROM windows WHERE status='pending'").fetchone()))
        inconsistent = settings.get('discovery_status') == 'inconsistent'
        pending = db.execute("SELECT 1 FROM windows WHERE status='pending'").fetchone()
        if refresh and (complete or (inconsistent and not pending)):
            with db:
                for table in ('pages', 'window_items', 'windows'):
                    db.execute(f'DELETE FROM {table}')
                db.execute("DELETE FROM settings WHERE key IN ('through','supplements_scheduled')")
                db.execute("INSERT OR REPLACE INTO settings VALUES ('discovery_status','collecting')")
            initialize(db, year)
            complete = False
        elif inconsistent:
            raise QueryError('拆分窗口总数不一致，已保留记录；请用--refresh重新查询')
        elif refresh and not complete:
            LOG.info('继续尚未完成的清单轮次，保留分页进度')
        if not complete:
            with db:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('discovery_status','collecting')")
            ensure_stock_cache(root, http)
            collect(db, http)
            if not db.execute("SELECT 1 FROM settings WHERE key='supplements_scheduled'").fetchone():
                schedule_supplements(db, year, root, http)
                collect(db, http)
        inventory = load_inventory(db, year, root)
        export(db, year, root, inventory)
        if not complete:
            with db:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('discovery_status','complete')")
        through = db.execute("SELECT value FROM settings WHERE key='through'").fetchone()[0]
        status = db.execute("SELECT value FROM settings WHERE key='discovery_status'").fetchone()
        LOG.info('年度%d清单%s；披露截止日through=%s；discovery_status=%s', year,
                 '复用已有记录' if complete else '发现完成', through,
                 status[0] if status else 'legacy_complete')
        return inventory
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description='准备历史A股年度报告清单，不下载PDF')
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--refresh', action='store_true', help='显式重新查询；中断后继续当前轮次')
    args = parser.parse_args()
    if not 2001 <= args.year < datetime.now(SHANGHAI).year:
        parser.error('请指定已经结束的会计年度（2001年起）')
    root = PROJECT / 'data'
    with run_lock(root):
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                            handlers=[logging.StreamHandler(), RotatingFileHandler(root / 'run.log',
                                      maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')], force=True)
        http = HttpClient()
        try:
            _, _, review = prepare_inventory(args.year, root, http, args.refresh)
            return int(bool(review))
        except (QueryError, StopBatch, requests.RequestException, KeyboardInterrupt) as exc:
            LOG.error('名单未完成，已保存分页进度：%s', exc)
            return 1
        finally:
            http.close()


if __name__ == '__main__':
    raise SystemExit(main())
