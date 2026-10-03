"""Evidence-first discovery of Korean local income tax departments.

Automatic output is a candidate, never a confirmed recipient. Hierarchy is
accepted only from a nested organization block containing both bureau and
department, not from two unrelated words in a whole-page navigation menu.
"""
import collections
import html
import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser

KST = timezone(timedelta(hours=9))
TAX = re.compile(r"지방\s*소득세")
DEPT = re.compile(r"(?:세무[0-9일이삼]*과|세정과|세정담당관|세입관리과|징수과|세무관리과|재무과|재정과|회계세무과|재무세무과|재정세무과|세무징수과|세원관리과|세원정보과|세정징수과|세정[0-9]*과)")
BUREAU = re.compile(r"[가-힣]{2,16}국")
PHONE = re.compile(r"0\d{1,2}[-) ]\s*\d{3,4}[- ]\d{4}")
PROVINCES = ["seoul", "busan", "daegu", "incheon", "gwangju", "daejeon", "ulsan", "gyeonggi", "gangwon", "chungbuk", "chungnam", "jeonbuk", "jeonnam", "gyeongbuk", "gyeongnam", "jeju", "sejong"]
REGION_WORDS = {"서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천", "광주광역시": "광주", "대전광역시": "대전", "울산광역시": "울산", "경기도": "경기", "강원특별자치도": "강원", "강원도": "강원", "충청북도": "충북", "충청남도": "충남", "전북특별자치도": "전북", "전라북도": "전북", "전라남도": "전남", "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주", "세종특별자치시": "세종"}
# Homepage discovery hints, not department answers. Both names must still be
# checked against current page identity; aliases are not nationwide rewrites.
HINTS = [{"name": "전남광주통합특별시 서구", "aliases": ["광주광역시 서구", "광주 서구", "광주서구"], "url": "https://www.seogu.gwangju.kr/", "org": "https://www.seogu.gwangju.kr/menu.es?mid=a10106030000"}]


def stamp():
    return datetime.now(KST).isoformat(timespec="seconds")


def compact(value):
    return re.sub(r"\s+", "", value or "")


def location_key(value):
    result = compact(value).removesuffix("청")
    for name, alias in REGION_WORDS.items():
        result = result.replace(name, alias)
    return result


def official_host(host):
    host = (host or "").lower().rstrip(".")
    return host.endswith((".go.kr", ".gov.kr")) or any(host.endswith("." + p + ".kr") for p in PROVINCES)


def validate_url(url, api=False):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or p.username or p.password or p.port not in (None, 80, 443):
        raise ValueError("일반 공식 홈페이지 URL만 조회할 수 있습니다.")
    if not ((p.scheme == "https" and p.hostname in ("openapi.naver.com", "apis.data.go.kr")) if api else official_host(p.hostname)):
        raise ValueError("공식 지자체 도메인이 아닌 주소는 자동 조회하지 않습니다.")
    for addr in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80)):
        if not ipaddress.ip_address(addr[4][0]).is_global:
            raise ValueError("내부망 주소는 조회하지 않습니다.")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, api=False):
        self.api = api

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.api:
            raise ValueError("검색 API의 예상하지 못한 리디렉션을 거부했습니다.")
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(url, headers=None, api=False):
    validate_url(url, api)
    context = ssl.create_default_context()
    # Retain chain and hostname verification. Python 3.13's extra X509 strict
    # flag rejects several legacy municipal certificates lacking AKI.
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    opener = urllib.request.build_opener(SafeRedirect(api), urllib.request.HTTPSHandler(context=context))
    req = urllib.request.Request(url, headers={"User-Agent": "DepartmentFinder/0.1 (+local research tool)", **(headers or {})})
    with opener.open(req, timeout=12) as response:
        data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("페이지 크기가 조회 한도를 초과했습니다.")
        charset = response.headers.get_content_charset() or ""
        mime = response.headers.get("Content-Type", "")
        if not api and "html" not in mime and not data.lstrip().startswith((b"<", b"<!")):
            raise ValueError("HTML 업무안내 페이지만 자동 분석합니다. 첨부파일은 직접 확인해 주세요.")
        if not charset:
            match = re.search(br"charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", data[:5000], re.I)
            charset = match.group(1).decode("ascii") if match else "utf-8"
        try:
            text = data.decode(charset)
        except (UnicodeDecodeError, LookupError):
            text = data.decode("cp949", errors="replace")
        return text, response.geturl()


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    parent: object = None
    children: list = field(default_factory=list)

    def text(self):
        if self.tag in ("script", "style", "noscript", "template"):
            return ""
        return " ".join(c if isinstance(c, str) else c.text() for c in self.children).strip()

    def walk(self, tag=None):
        if tag is None or self.tag == tag:
            yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk(tag)


class Page(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.current = self.root
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        n = Node(tag, dict(attrs), self.current)
        self.current.children.append(n)
        if tag not in self.VOID:
            self.current = n

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        n = self.current
        while n.parent is not None:
            if n.tag == tag:
                self.current = n.parent
                return
            n = n.parent

    def handle_data(self, data):
        self.current.children.append(data)

    def identity(self):
        values = [n.text() for n in self.root.walk() if n.tag in ("title", "h1", "footer") or re.search(r"logo|footer|copyright", n.attrs.get("id", ""), re.I)]
        return " ".join(values)

    def links(self, url):
        for n in self.root.walk("a"):
            href = n.attrs.get("href", "")
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                yield n.text(), urllib.parse.urljoin(url, href), n


def hierarchy(page, url):
    results = {}
    for label, link, n in page.links(url):
        dept = DEPT.fullmatch(compact(label))
        if not dept:
            continue
        current = n.parent
        # A department's own LI, then enclosing UL and bureau LI. Never go
        # outside four levels into a whole navigation container.
        for _ in range(4):
            if current is None:
                break
            if current.tag == "li":
                first = next(current.walk("a"), None)
                bureau = compact(first.text()) if first else ""
                if BUREAU.fullmatch(bureau):
                    results.setdefault(dept.group(), []).append({"bureau": bureau, "url": url, "department_url": link, "evidence": f"{bureau} → {dept.group()}（조직 구조）"})
                    break
            current = current.parent
    return results


def content_root(page):
    options = [n for n in page.root.walk() if n.attrs.get("id", "").lower() in ("contents", "content", "sub_content", "subcontent", "content_area", "contents_body") or n.tag == "main"]
    return options[0] if options else page.root


def extract_candidates(page, url, department_hint="", bureau_hint="", kind="all"):
    root = content_root(page)
    title = next(page.root.walk("title"), None)
    title_depts = DEPT.findall(title.text()) if title else []
    # A page may have many departments in its sidebar. Only title/headings
    # containing exactly one distinct tax department can supply a context.
    heading_depts = []
    for n in root.walk():
        if n.tag in ("h2", "h3", "h4"):
            heading_depts.extend(DEPT.findall(n.text()))
    context = department_hint
    if not context and len(set(title_depts + heading_depts)) == 1:
        context = (title_depts + heading_depts)[0]
    candidates = []
    for node in root.walk():
        if node.tag not in ("tr", "p", "li", "dd"):
            continue
        text = re.sub(r"\s+", " ", node.text())
        if not TAX.search(text) or len(text) > 1700:
            continue
        parent = node.parent
        in_nav = False
        while parent:
            if parent.tag in ("nav", "header", "footer"):
                in_nav = True
            parent = parent.parent
        if in_nav:
            continue
        depts = list(dict.fromkeys(DEPT.findall(text)))
        if len(depts) > 1:
            continue
        dept = depts[0] if depts else context
        if not dept:
            continue
        phones = PHONE.findall(text)
        teams = re.findall(r"[가-힣]{2,12}팀", text)
        if not teams:
            scope = node.parent
            for _ in range(5):
                if not scope:
                    break
                headings = [h.text() for h in scope.walk("h3")]
                if len(headings) == 1:
                    teams = re.findall(r"[가-힣]{2,12}팀", headings[0])
                    break
                scope = scope.parent
        cells = [c for c in node.children if isinstance(c, Node) and c.tag == "td"]
        duty = cells[-1].text() if len(cells) >= 3 else text
        score = 3 + (3 if node.tag == "tr" else 0) + (2 if phones else 0)
        if re.search(r"지방소득세\s*(?:업무\s*)?(?:전반|총괄)", text):
            score += 4
        if any(w in text for w in ("세입이체", "세입 이체", "타시군", "타 시군", "자치단체간", "자치단체 간")):
            score += 6
        if kind != "all" and {"personal": "개인", "corporate": "법인", "special": "특별징수"}[kind] in text:
            score += 3
        # Show personal/corporate rows independently when departments differ.
        candidates.append({"department": dept, "bureau": bureau_hint, "team": teams[0] if teams else "", "phone": phones[0] if phones else "", "duty": duty, "url": url, "score": score, "evidence": [{"type": "업무안내", "url": url, "text": text[:900]}]})
    return candidates


def search(query, credentials):
    url = "https://openapi.naver.com/v1/search/webkr.json?" + urllib.parse.urlencode({"query": query, "display": 8})
    try:
        body, _ = request(url, {"X-Naver-Client-Id": credentials[0], "X-Naver-Client-Secret": credentials[1]}, api=True)
        items = json.loads(body).get("items", [])
        return [{"url": i["link"], "title": html.unescape(re.sub("<[^>]+>", "", i.get("title", ""))), "description": html.unescape(re.sub("<[^>]+>", "", i.get("description", "")))} for i in items if official_host(urllib.parse.urlsplit(i.get("link", "")).hostname)]
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("네이버 인증에 실패했습니다. Client ID/Secret 및 검색 API 사용 설정을 확인해 주세요.") from None
        if exc.code == 429:
            raise ValueError("네이버 검색 호출 한도를 초과했습니다. 잠시 후 다시 조회해 주세요.") from None
        raise ValueError(f"네이버 검색 API 오류: HTTP {exc.code}") from None


def matching_identity(location, page, hint=None):
    identity = page.identity()
    if hint:
        return any(location_key(n) in location_key(identity) for n in [hint["name"], *hint["aliases"]])
    return location_key(location) in location_key(identity)


def find_department(location, credentials=("", ""), kind="all", source_url="", public_key=""):
    location = re.sub(r"\s+", " ", location).strip()
    result = {"location": location, "canonical": location, "status": "검토 필요", "candidates": [], "notes": [], "checked_at": stamp(), "confirmed": False}
    if not location or len(location) > 100:
        result["notes"].append("지자체 이름을 확인해 주세요.")
        return result
    if compact(location).removesuffix("청") in ("서구", "동구", "남구", "북구", "중구", "고성군", "광주시", "강서구"):
        result["notes"].append("동명이 있는 지자체입니다. 시·도를 포함해 입력해 주세요.")
        return result
    hint = next((h for h in HINTS if location_key(location) in [location_key(n) for n in [h["name"], *h["aliases"]]]), None)
    if hint:
        result["canonical"] = hint["name"]
        if location_key(location) != location_key(hint["name"]):
            result["notes"].append("구 명칭으로 검색했습니다. 현재 공식 홈페이지의 기관명과 함께 확인해 주세요.")
    queries = [f'{location} 지방소득세 담당 직원 업무', f'{location} 세무과 조직도']
    result["search_url"] = "https://search.naver.com/search.naver?" + urllib.parse.urlencode({"query": queries[0]})
    queue = collections.deque()
    if source_url:
        if not official_host(urllib.parse.urlsplit(source_url).hostname):
            result["notes"].append("공식 지자체 홈페이지 주소를 입력해 주세요.")
            return result
        queue.append((source_url, "", "", True))
    if hint:
        queue.append((hint["org"], "", "", True))
    if all(credentials):
        items = []
        try:
            for query in queries:
                items.extend(search(query, credentials))
            for item in items:
                queue.append((item["url"], "", "", False))
        except Exception as exc:
            result["notes"].append(str(exc) if isinstance(exc, ValueError) else "검색 서버에 연결할 수 없습니다. 네트워크를 확인해 주세요.")
    elif not hint and not source_url and not public_key:
        result["status"] = "API 설정 필요"
        result["notes"].append("네이버 검색 API를 설정하거나 공식 업무안내 URL을 입력해 주세요.")
        return result
    org_candidates = []
    if public_key:
        from public_api import OrgClient
        try:
            org_candidates, org_notes = OrgClient(public_key).candidates(result["canonical"])
            result["notes"].extend(org_notes)
        except ValueError as exc:
            result["notes"].append(str(exc))
    visited, pages, all_candidates, relations = set(), {}, [], {}
    trusted_hosts = set()
    while queue and len(visited) < 12:
        url, dept_hint, bureau_hint, trusted = queue.popleft()
        if url in visited or re.search(r"download|\.pdf(?:\?|$)|\.hwp(?:x)?(?:\?|$)", url, re.I):
            continue
        visited.add(url)
        try:
            raw, final_url = request(url)
            page = Page(raw)
            host = urllib.parse.urlsplit(final_url).hostname
            # Parent-proven staff fragments have no institution header. Other
            # pages always require matching institution identity, including
            # search engine results on a generic government portal.
            if trusted and host in trusted_hosts:
                pass
            elif matching_identity(location, page, hint):
                trusted_hosts.add(host)
            else:
                continue
            pages[final_url] = page
            mapping = hierarchy(page, final_url)
            for dept, entries in mapping.items():
                relations.setdefault(dept, []).extend(entries)
                for entry in entries:
                    queue.appendleft((entry["department_url"], dept, entry["bureau"], True))
            all_candidates.extend(extract_candidates(page, final_url, dept_hint, bureau_hint, kind))
            # A specific official site's department view loads its public
            # employee table using AJAX. Follow only active department IDs.
            if host == "www.seogu.gwangju.kr" and dept_hint:
                for node in page.root.walk("div"):
                    if "active" in node.attrs.get("class", "").split() and re.fullmatch(r"\d{7}", node.attrs.get("id", "")) and dept_hint in compact(node.text()):
                        fragment_url = urllib.parse.urljoin(final_url, "/organizationMemberLowList.es?org_cd=" + node.attrs["id"])
                        queue.appendleft((fragment_url, dept_hint, bureau_hint, True))
            follow = []
            for label, link, node in page.links(final_url):
                if urllib.parse.urlsplit(link).hostname != host:
                    continue
                if re.search(r"조직도|행정조직|직원.*(?:안내|검색)|업무.*안내|세무.*과|세정과", label) and len(label) < 35:
                    follow.append((link, "", "", True))
            # Organization and tax detail pages first; avoid crawling every
            # personnel page or unrelated content on a municipal portal.
            for entry in follow[:6]:
                queue.append(entry)
        except Exception as exc:
            if len(result["notes"]) < 5:
                if isinstance(exc, ValueError):
                    note = str(exc)
                elif isinstance(exc, urllib.error.HTTPError):
                    note = f"공식 홈페이지 조회 오류: HTTP {exc.code}"
                else:
                    note = "일부 공식 페이지를 읽지 못했습니다. 접속 제한 또는 네트워크를 확인해 주세요."
                if note not in result["notes"]:
                    result["notes"].append(note)
    unique = {}
    for candidate in all_candidates:
        candidate["duty_verified"] = True
        entries = relations.get(candidate["department"], [])
        bureaus = set(e["bureau"] for e in entries)
        if len(bureaus) == 1:
            candidate["bureau"] = next(iter(bureaus))
            candidate["evidence"].append({"type": "조직도", "url": entries[0]["url"], "text": entries[0]["evidence"]})
        elif len(bureaus) > 1:
            candidate["bureau"] = ""
            candidate["evidence"].append({"type": "조직 상충", "url": entries[0]["url"], "text": "상위 국이 여러 개여서 직접 확인이 필요합니다."})
        # A hint is context for loading the staff table, not independent proof
        # in an unverified page. Nested relation is required for the bureau.
        elif not entries:
            candidate["bureau"] = ""
        key = (candidate["department"], candidate["phone"], candidate["duty"])
        unique.setdefault(key, candidate)
    # Match only when the organization API has one branch with that name.
    # Repeated tax departments under separate districts stay separate.
    matched = set()
    for candidate in unique.values():
        matches = [c for c in org_candidates if c["department"] == candidate["department"]]
        if len(matches) != 1:
            continue
        org = matches[0]
        parts = org["org_full_name"].split()
        boundary = next((i for i in range(1, len(parts) + 1) if location_key(" ".join(parts[:i])) == location_key(result["canonical"])), None)
        # A city-wide staff page cannot prove it belongs to one of the city's
        # district branches merely because only one branch was returned.
        if boundary is None or any(re.fullmatch(r"[가-힣]+(?:시|군|구)", p) for p in parts[boundary:-1]):
            continue
        candidate["org_code"] = org["org_code"]
        candidate["org_full_name"] = org["org_full_name"]
        candidate["evidence"].extend(org["evidence"])
        matched.add(org["org_code"])
        if candidate["bureau"] and org["bureau"] and candidate["bureau"] != org["bureau"]:
            candidate["bureau"] = ""
            result["notes"].append("홈페이지와 기관코드의 상위 국이 다릅니다. 최신 조직을 직접 확인해 주세요.")
        elif not candidate["bureau"] and not any(e["type"] == "조직 상충" for e in candidate["evidence"]):
            candidate["bureau"] = org["bureau"]
    combined = list(unique.values()) + [c for c in org_candidates if c["org_code"] not in matched]
    result["candidates"] = sorted(combined, key=lambda x: -x["score"])[:20]
    if len(combined) > 20:
        result["notes"].append("후보가 많아 상위 20개만 표시했습니다. 시·군·구 이름을 더 구체적으로 입력해 주세요.")
    if result["candidates"]:
        depts = {c["department"] for c in result["candidates"]}
        duty_found = any(c.get("duty_verified") for c in result["candidates"])
        result["status"] = "후보 발견" if duty_found else "조직 후보"
        if not duty_found:
            result["notes"].append("기관코드는 조직 정보만 제공합니다. 공식 업무안내 URL로 다시 조회하거나 네이버 검색 API를 연결해 지방소득세·세입 이체 업무를 확인해 주세요.")
        if len(depts) > 1:
            result["notes"].append("관련 부서가 여러 개입니다. 개인·법인·세입 이체 업무를 비교해 수신처를 선택해 주세요.")
        if not any(c["bureau"] for c in result["candidates"]):
            result["notes"].append("상위 국은 근거를 찾지 못해 비워 두었습니다. 조직도를 확인해 주세요.")
        if duty_found and not any(c.get("duty_verified") and "이체" in c["duty"] for c in result["candidates"]):
            result["notes"].append("지방소득세 업무는 찾았지만 세입 이체 담당인지는 추가 확인이 필요합니다.")
    else:
        result["notes"].append("지방소득세 담당 부서를 본문에서 확인하지 못했습니다. 검색 링크 또는 공식 URL로 보완해 주세요.")
    result["pages_checked"] = len(pages)
    return result
