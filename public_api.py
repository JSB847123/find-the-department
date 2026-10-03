"""MOIS institution-code API. Never persist or return a key-bearing URL."""
import json
import re
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

ENDPOINT = "https://apis.data.go.kr/1741000/StanOrgCd2/getStanOrgCdList2"
SOURCE = "https://www.data.go.kr/data/15077870/openapi.do"


def api_error(code):
    if code in ("30", "31", "32", "401", "403", "ERROR-290", "ERROR-300"):
        return "기관코드 API 인증에 실패했습니다. 일반 인증키와 해당 API의 활용신청 승인 여부를 확인해 주세요."
    if code in ("22", "429"):
        return "기관코드 API 호출 한도를 초과했습니다. 공공데이터포털의 이용 현황을 확인해 주세요."
    return "기관코드 API가 오류를 반환했습니다. 공공데이터포털의 서비스 상태와 활용신청을 확인해 주세요."


def parse_response(body):
    rows, total, codes = [], None, []
    try:
        if body.lstrip().startswith("<"):
            root = ET.fromstring(body)
            for node in root.iter():
                name = node.tag.rsplit("}", 1)[-1]
                if name in ("resultCode", "returnReasonCode"):
                    codes.append((node.text or "").strip())
                if name == "totalCount":
                    total = int(node.text or "0")
                fields = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in node}
                if "org_cd" in fields:
                    rows.append(fields)
        else:
            def walk(node):
                nonlocal total
                if isinstance(node, dict):
                    if "org_cd" in node:
                        rows.append({k: str(v or "").strip() for k, v in node.items() if not isinstance(v, (dict, list))})
                    for k, v in node.items():
                        if k in ("resultCode", "returnReasonCode"):
                            codes.append(str(v))
                        if k == "totalCount":
                            total = int(v)
                        walk(v)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)
            walk(json.loads(body))
    except (ValueError, TypeError, ET.ParseError):
        raise ValueError("기관코드 API 응답 형식을 읽지 못했습니다.") from None
    if any(c not in ("INFO-0", "INFO-200", "00", "0") for c in codes):
        raise ValueError(api_error(next(c for c in codes if c not in ("INFO-0", "INFO-200", "00", "0"))))
    if not codes and not rows and total is None:
        raise ValueError("기관코드 API에서 예상한 조회 응답을 받지 못했습니다.")
    return [r for r in rows if r.get("stop_selt", "0") == "0"], total


class OrgClient:
    def __init__(self, key):
        self.key = urllib.parse.unquote(key.strip())
        self.cache = {}
        self.calls = 0

    def page(self, **params):
        from finder import request
        if self.calls >= 40:
            raise ValueError("기관코드 조회 한도에 도달했습니다. 일부 상위 조직은 추가 확인이 필요합니다.")
        self.calls += 1
        url = ENDPOINT + "?" + urllib.parse.urlencode({"ServiceKey": self.key, "pageNo": 1, "numOfRows": 100, "type": "json", "stop_selt": "0", **params})
        try:
            body, _ = request(url, api=True)
        except urllib.error.HTTPError as exc:
            raise ValueError(api_error(str(exc.code))) from None
        except Exception:
            raise ValueError("기관코드 API에 연결하지 못했습니다. 네트워크와 서비스 상태를 확인해 주세요.") from None
        return parse_response(body)

    def lookup(self, code):
        if code not in self.cache:
            rows, _ = self.page(org_cd=code)
            self.cache[code] = next((r for r in rows if r.get("org_cd") == code), None)
        return self.cache[code]

    def candidates(self, location):
        from finder import DEPT, BUREAU, REGION_WORDS, location_key, compact
        official = location.strip().removesuffix("청")
        first, space, rest = official.partition(" ")
        expansion = next((name for name, alias in REGION_WORDS.items() if first == alias), first)
        official = expansion + (space + rest if space else "")
        rows, notes, seen = [], [], set()
        for page_no in range(1, 21):
            page, total = self.page(full_nm=official, pageNo=page_no)
            new = [r for r in page if r.get("org_cd") not in seen]
            rows.extend(new)
            seen.update(r.get("org_cd") for r in new)
            if not page or (total is not None and page_no * 100 >= total) or (total is None and len(page) < 100):
                break
            if not new or page_no == 20:
                notes.append("기관코드 결과가 많거나 페이지가 반복돼 조회를 제한했습니다. 누락된 조직이 있을 수 있습니다.")
                break
        for row in rows:
            self.cache[row["org_cd"]] = row
        # Require the requested municipality as a complete prefix, not merely
        # a substring (e.g. 서울 중구 must never accept 부산 중구).
        scope = location_key(location)
        def in_scope(row):
            parts = row.get("full_nm", "").split()
            return any(location_key(" ".join(parts[:i])) == scope for i in range(1, len(parts) + 1))
        scoped = [r for r in rows if in_scope(r)]
        roots = [r for r in scoped if location_key(r.get("full_nm", "")) == scope]
        root_codes = {r["org_cd"] for r in roots}
        candidates = []
        for row in scoped:
            dept = compact(row.get("low_nm", ""))
            if not DEPT.fullmatch(dept):
                continue
            parent_code, visited, chain, bureau = row.get("high_cd", ""), {row["org_cd"]}, [], ""
            try:
                for _ in range(10):
                    if not re.fullmatch(r"\d{7}", parent_code) or parent_code == "0000000" or parent_code in visited or parent_code in root_codes:
                        break
                    visited.add(parent_code)
                    parent = self.lookup(parent_code)
                    if not parent or not in_scope(parent):
                        break
                    chain.append(parent)
                    if not bureau and BUREAU.fullmatch(compact(parent.get("low_nm", ""))):
                        bureau = compact(parent["low_nm"])
                    parent_code = parent.get("high_cd", "")
            except ValueError as exc:
                if str(exc) not in notes:
                    notes.append(str(exc))
            evidence = [{"type": "기관코드 API", "url": SOURCE, "text": f"{row['full_nm']} · 기관코드 {row['org_cd']} · 상위기관코드 {row.get('high_cd', '')}"}]
            evidence.extend({"type": "상위기관 코드", "url": SOURCE, "text": f"{p['full_nm']} · 기관코드 {p['org_cd']} · 상위기관코드 {p.get('high_cd', '')}"} for p in chain)
            candidates.append({"department": dept, "bureau": bureau, "team": "", "phone": "", "duty": "담당업무 미확인: 기관코드에서 찾은 세무 관련 조직입니다. 지방소득세·세입 이체 담당 여부를 공식 업무안내에서 확인해 주세요.", "duty_verified": False, "org_code": row["org_cd"], "org_full_name": row["full_nm"], "url": SOURCE, "score": 1, "evidence": evidence})
        if not candidates:
            notes.append("기관코드에서 해당 지자체의 세무 관련 하위 조직을 찾지 못했습니다. 공식 명칭과 홈페이지를 확인해 주세요.")
        return candidates, notes


def check_connection(key):
    rows, _ = OrgClient(key).page(org_cd="1741000", numOfRows=1)
    if not any(r.get("org_cd") == "1741000" for r in rows):
        raise ValueError("API 응답은 받았지만 행정안전부 기관코드를 확인하지 못했습니다.")
    return "기관코드 API 연결 성공: 행정안전부 기관코드를 확인했습니다."
