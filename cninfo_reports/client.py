"""CNINFO 查询与低频 HTTP；部分查询逻辑改编自 cninfo-mcp（MIT）。

上游：youhaozhao/cninfo-mcp，90723c078af902e42f9b8be0ea476fa688c585f3。
许可及改编说明见项目 THIRD_PARTY_NOTICES.md。
"""

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests


STOCK_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
TOP_SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_URL = "https://static.cninfo.com.cn/"
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
PAGE_SIZE = 30
CACHE_SECONDS = 7 * 24 * 60 * 60
NON_FULLTEXT = re.compile(r"摘要|英文|英语|English|半年度|半年报|关于|公告|意见|说明|审计|董事会|监事会", re.I)
WITHDRAWN_SUFFIX = re.compile(r"[（(]已取消[）)]$")
LOG = logging.getLogger(__name__)


class StopBatch(Exception):
    """网站要求停止访问，调用方必须停止整个批次。"""


class QueryError(Exception):
    """查询未完整完成，不能当作无报告。"""


class IdentityError(Exception):
    """股票代码无法精确对应公司。"""


@dataclass(frozen=True)
class Report:
    org_id: str
    stock_code: str
    company_name: str
    report_year: int
    announcement_id: str
    title: str
    announcement_date: str
    url: str


def page_has_more(data, page, fetched_count, item_count):
    """按总记录数判定末页，兼容接口 totalpages 向下或向上取整。"""
    total_count = data.get("totalRecordNum", data.get("totalAnnouncement"))
    total_pages = data.get("totalpages")
    more = data.get("hasMore")
    if total_pages is not None and (type(total_pages) is not int or total_pages < 0):
        raise QueryError(f"第 {page} 页总页数无效")
    if total_count is not None:
        if type(total_count) is not int or total_count < 0 or fetched_count > total_count:
            raise QueryError(f"第 {page} 页公告总数无效或已返回数量超过总数")
        allowed_pages = {total_count // PAGE_SIZE, (total_count + PAGE_SIZE - 1) // PAGE_SIZE}
        if total_pages is not None and total_pages not in allowed_pages:
            raise QueryError("公告总数与总页数相互矛盾，查询未完成")
        expected = fetched_count < total_count
        if isinstance(more, bool) and more != expected:
            raise QueryError("公告总数与 hasMore 相互矛盾，查询未完成")
        more = expected
    elif not isinstance(more, bool):
        more = item_count == PAGE_SIZE
    if more and (not item_count or (total_count is not None and item_count < PAGE_SIZE)):
        raise QueryError("公告接口声称仍有结果但返回空页或不足一页，分页未完成")
    return more


def _retry_after(value):
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    try:
        return max(0, parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError):
        return 0


class HttpClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Referer": "https://www.cninfo.com.cn/"})
        self.next_request = 0.0
        self.rate_limits = 0

    def request(self, method, url, consume, headers_factory=None, **kwargs):
        kwargs.update(timeout=(10, 60), stream=True, allow_redirects=False)
        for attempt in range(3):
            wait = self.next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            response = None
            retry_delay = 15 * (attempt + 1)
            retry_after = 0
            LOG.info("HTTP 开始 %s %s（第 %d 次尝试）", method, url, attempt + 1)
            try:
                request_kwargs = kwargs
                if headers_factory is not None:
                    request_kwargs = {**kwargs, "headers": {**(kwargs.get("headers") or {}),
                                                           **headers_factory()}}
                response = self.session.request(method, url, **request_kwargs)
                if response.status_code == 403:
                    raise StopBatch("网站返回 HTTP 403，已停止批次")
                if response.status_code == 429:
                    retry_after = _retry_after(response.headers.get("Retry-After"))
                    self.rate_limits += 1
                    if self.rate_limits >= 2:
                        raise StopBatch("网站第二次返回 HTTP 429，已停止批次")
                    retry_delay = max(retry_delay, retry_after)
                    response.raise_for_status()
                if 300 <= response.status_code < 400:
                    raise requests.HTTPError("拒绝自动跟随重定向", response=response)
                content_type = response.headers.get("Content-Type", "").lower()
                if "html" in content_type or "json" in content_type:
                    if re.search(r"验证码|人机验证|安全验证|captcha", response.text, re.I):
                        raise StopBatch("网站返回验证页面，已停止批次")
                response.raise_for_status()
                return consume(response)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status != 429 and status < 500:
                    raise
                if attempt == 2:
                    raise
                LOG.warning("HTTP 暂时失败，将退避重试：%s", exc)
            except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                if attempt == 2:
                    raise
                LOG.warning("网络读取失败，将退避重试：%s", exc)
            finally:
                if response is not None:
                    response.close()
                self.next_request = time.monotonic() + max(random.uniform(5, 7), retry_after)
                LOG.info("HTTP 结束 %s %s status=%s", method, url,
                         response.status_code if response is not None else "network_error")
            time.sleep(retry_delay)

    def json(self, url, data=None):
        def consume(response):
            try:
                return response.json()
            except requests.exceptions.JSONDecodeError as exc:
                raise QueryError("网站响应不是有效 JSON") from exc

        return self.request("GET" if data is None else "POST", url, consume,
                            **({} if data is None else {"data": data}))

    def close(self):
        self.session.close()


def _is_a_share(code, category=None):
    return (bool(re.fullmatch(r"[0-9]{6}", code))
            and code.startswith(("0", "3", "6", "4", "8", "92"))
            and not code.startswith("689")
            and (category is None or category == "A股"))


def _company(stocks, entry):
    org_id = str(entry["orgId"])
    return {
        "code": str(entry["code"]),
        "org_id": org_id,
        "name": entry.get("zwjc") or entry.get("name") or entry.get("orgName") or "",
        "aliases": sorted({str(row["code"]) for row in stocks if str(row.get("orgId")) == org_id
                           and _is_a_share(str(row["code"]), row.get("category"))}),
    }


class CninfoClient:
    def __init__(self, http, cache_path: Path, reviewed_years=None, excluded_reports=None):
        self.http = http
        self.cache_path = Path(cache_path)
        self.reviewed_years = reviewed_years or {}
        self.excluded_reports = excluded_reports or set()
        self.stocks = None
        self.fetched_at = 0
        self.refreshed = False

    def _save_cache(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps({"fetched_at": self.fetched_at, "stockList": self.stocks},
                                            ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.cache_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _refresh(self):
        data = self.http.json(STOCK_URL)
        if not isinstance(data, dict) or not isinstance(data.get("stockList"), list):
            raise QueryError("股票字典响应缺少 stockList")
        self.stocks = [row for row in data["stockList"]
                       if re.fullmatch(r"[0-9]{6}", str(row.get("code", ""))) and row.get("orgId")]
        self.fetched_at = time.time()
        self.refreshed = True
        self._save_cache()

    def resolve(self, code):
        if not re.fullmatch(r"[0-9]{6}", code):
            raise IdentityError("股票代码必须为六位数字字符串")
        if not _is_a_share(code):
            raise IdentityError(f"{code} 不属于支持的沪深北 A 股代码段")
        if self.stocks is None:
            try:
                cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached = {}
            if (isinstance(cached, dict) and isinstance(cached.get("stockList"), list)
                    and isinstance(cached.get("fetched_at"), (int, float))
                    and 0 <= time.time() - cached["fetched_at"] < CACHE_SECONDS):
                self.stocks, self.fetched_at = cached["stockList"], cached["fetched_at"]
            else:
                self._refresh()
        entry = next((row for row in self.stocks if row.get("code") == code), None)
        if entry is None and not self.refreshed:
            self._refresh()
            entry = next((row for row in self.stocks if row.get("code") == code), None)
        new_entry = entry is None
        if new_entry:
            hits = self.http.json(TOP_SEARCH_URL, {"keyWord": code, "maxNum": 10})
            if not isinstance(hits, list):
                raise QueryError("股票精确检索响应不是列表")
            entry = next((row for row in hits if str(row.get("code")) == code and row.get("orgId")), None)
            if entry is None:
                raise IdentityError(f"未找到 {code} 的精确公司身份；不采用搜索首条推测")
        if not _is_a_share(code, entry.get("category")):
            raise IdentityError(f"{code} 的证券类别不是 A 股")
        if new_entry:
            self.stocks.append(entry)
            self._save_cache()
        return _company(self.stocks, entry)

    def reports(self, company, years: set[int]):
        if not years:
            return []
        code = company["code"]
        if not _is_a_share(code):
            raise IdentityError(f"{code} 不属于支持的沪深北 A 股代码段")
        if code.startswith("6"):
            plate = "sh"
        elif code.startswith(("0", "3")):
            plate = "sz"
        else:
            plate = "bj"
        categories = {str(row["code"]): row.get("category") for row in self.stocks or []}
        found, pages_seen, fetched_count = [], set(), 0
        for page in range(1, 101):
            payload = {"pageNum": page, "pageSize": PAGE_SIZE, "tabName": "fulltext",
                       "column": "szse", "plate": plate, "stock": f"{code},{company['org_id']}",
                       "category": "category_ndbg_szsh", "searchkey": "", "isHLtitle": "false",
                       "seDate": f"{min(years)}-01-01~{datetime.now(SHANGHAI).date().isoformat()}"}
            try:
                data = self.http.json(QUERY_URL, payload)
            except requests.RequestException as exc:
                raise QueryError(f"{code} 第 {page} 页查询失败：{exc}") from exc
            if not isinstance(data, dict) or "announcements" not in data:
                raise QueryError(f"{code} 第 {page} 页缺少公告列表")
            items = data["announcements"] or []
            if not isinstance(items, list):
                raise QueryError("公告列表格式错误")
            fingerprint = tuple((str(item.get("announcementId", "")), item.get("adjunctUrl", "")) for item in items)
            if items and fingerprint in pages_seen:
                raise QueryError("公告接口重复返回同一页，分页未完成")
            pages_seen.add(fingerprint)
            fetched_count += len(items)
            for item in items:
                title = _clean_title(item.get("announcementTitle", ""))
                org_id = str(item.get("orgId") or "")
                announcement_id = str(item.get("announcementId") or "")
                if (announcement_id, org_id) in self.excluded_reports:
                    continue
                year = self.reviewed_years.get((announcement_id, org_id))
                if year is None:
                    year = _report_year(title)
                if year not in years:
                    continue
                sec_code = str(item.get("secCode") or "")
                if org_id and org_id != company["org_id"]:
                    continue
                if sec_code not in company["aliases"] and org_id != company["org_id"]:
                    continue
                if not re.fullmatch(r"[0-9]{6}", sec_code) or not re.fullmatch(r"[0-9]+", announcement_id):
                    raise QueryError(f"公告证券代码或公告 ID 无效：{title}")
                if not _is_a_share(sec_code, categories.get(sec_code)):
                    continue
                try:
                    published = item["announcementTime"]
                    if isinstance(published, (int, float)):
                        published = datetime.fromtimestamp(published / 1000, SHANGHAI).date().isoformat()
                    else:
                        published = date.fromisoformat(str(published)[:10]).isoformat()
                    url = _pdf_url(item["adjunctUrl"])
                except (KeyError, ValueError) as exc:
                    raise QueryError(f"公告日期或 PDF 地址无效：{title}") from exc
                found.append(Report(company["org_id"], sec_code,
                                    item.get("secName") or company["name"], year,
                                    announcement_id, title, published, url))
            if not page_has_more(data, page, fetched_count, len(items)):
                return found
        raise QueryError("公告分页超过 100 页，查询未完成")


def _clean_title(title):
    return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]*>", "", title)))


def _report_year(title):
    title = WITHDRAWN_SUFFIX.sub("", title)
    title = title.replace("（", "(").replace("）", ")").replace("_", "").strip("《》")
    title = title.replace("【", "(").replace("】", ")").removesuffix(".")
    title = title.replace("年度年度报告", "年度报告").replace("年度报告-", "年度报告").replace("年报报告", "年报")
    title = title.replace("全文-更新", "全文更新")
    title = re.sub(r"\(修订稿$", "(修订稿)", title)
    title = re.sub(r"\.docx$", "", title, flags=re.I)
    if NON_FULLTEXT.search(title):
        return None
    match = re.fullmatch(r".*?([0-9]{4})年?(?:年度报告|年度年报|年度定期报告全文|年报)(.*)", title)
    if not match:
        return None
    version = (r"(?:[0-9]{4}年)?(?:[0-9]{1,2}月)?(?:[0-9]{1,2}日)?"
               r"(?:[二三四]次|补充)?(?:修订|修正|更正|订正|更新)(?:版|前|后|稿)?")
    suffix = rf"(?:全文|正文|A股|标准版|可视版|图文版|印刷版|补充版|合并报告|已更新|{version})"
    if not re.fullmatch(rf"(?:{suffix}|\({suffix}\))*", match[2]):
        return None
    return int(match[1])


def _pdf_url(value):
    if not isinstance(value, str) or re.search(r"[\s\\]", value):
        raise ValueError("PDF 地址格式错误")
    parsed = urlsplit(urljoin(STATIC_URL, value))
    if (parsed.scheme != "https" or parsed.hostname != "static.cninfo.com.cn"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or not parsed.path.lower().endswith(".pdf")):
        raise ValueError("只接受巨潮静态站 HTTPS PDF 地址")
    return urlunsplit(("https", "static.cninfo.com.cn", parsed.path, parsed.query, ""))


def select_reports(reports, versions):
    if versions not in ("latest", "all"):
        raise ValueError("versions 必须为 latest 或 all")
    groups, seen_ids, seen_urls = {}, set(), set()
    for report in sorted(reports, key=lambda r: (r.announcement_date, r.announcement_id), reverse=True):
        if versions == "latest" and WITHDRAWN_SUFFIX.search(report.title):
            continue
        if (report.announcement_id and report.announcement_id in seen_ids) or report.url in seen_urls:
            continue
        seen_ids.add(report.announcement_id)
        seen_urls.add(report.url)
        groups.setdefault((report.org_id, report.report_year), []).append(report)
    selected, ambiguous = [], []
    for (org_id, year), group in sorted(groups.items()):
        if versions == "all":
            selected.extend(group)
            continue
        rank = lambda r: (r.announcement_date, bool(re.search(r"修订|修正|更正|订正|更新|补充版", r.title)))
        best = max(map(rank, group))
        candidates = [report for report in group if rank(report) == best]
        if len(candidates) == 1:
            selected.append(candidates[0])
        else:
            ambiguous.append({"org_id": org_id, "report_year": year,
                              "error": "同日存在多个同优先级完整版本，无法判定最新：" +
                              "；".join(f"{r.announcement_id} {r.title}" for r in candidates)})
    return selected, ambiguous
