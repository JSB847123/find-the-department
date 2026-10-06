"""Refresh public municipality URLs from the MOIS directory.

Run from the repository root: python scripts/update_website_catalog.py
Only public directory/homepage links are read; no API keys are used.
"""
import argparse
import collections
import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import finder

SOURCE = 'https://www.mois.go.kr/frt/sub/a04/localGovernment/screen.do'
DESTINATION = ROOT / 'catalog' / 'websites.json'
SUPPLEMENTAL = ROOT / 'catalog' / 'supplemental_websites.json'


def aliases(region, local):
    if region == '전남광주통합특별시':
        old = '광주광역시' if local.endswith('구') else '전라남도'
        short = '광주' if local.endswith('구') else '전남'
        return [f'{old} {local}', f'{short} {local}']
    if region == '제주특별자치도':
        return [f'제주도 {local}']
    return []


def clean_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        return ''
    parameters = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    stable = [(key, value) for key, value in parameters if key.lower() not in ('token', 'sessionid', 'jsessionid', 'csrf')]
    query = parsed.query if len(stable) == len(parameters) else urllib.parse.urlencode(stable)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or '/', query, ''))


def supplemental_entries(parents):
    entries = json.loads(SUPPLEMENTAL.read_text(encoding='utf-8'))['websites']
    parent_names = {finder.location_key(e['name']) for e in parents if e['level'] == '시군구'}
    for entry in entries:
        parent = ' '.join(entry['name'].split()[:-1])
        if entry['level'] != '일반구' or finder.location_key(parent) not in parent_names:
            raise ValueError('일반구의 소속 시를 공식 목록에서 확인해 주세요: ' + entry['name'])
        if not clean_url(entry['url']) or not clean_url(entry['source_url']):
            raise ValueError('일반구 홈페이지의 주소와 출처를 확인해 주세요: ' + entry['name'])
        if not finder.official_host(urllib.parse.urlsplit(entry['url']).hostname) or not finder.official_host(urllib.parse.urlsplit(entry['source_url']).hostname):
            raise ValueError('일반구 주소는 공식 자료를 근거로 등록해야 합니다.')
    return entries


def parse_directory(raw):
    page = finder.Page(raw)
    area = next((n for n in page.root.walk() if n.attrs.get('id') == 'print_area'), None)
    if not area:
        raise ValueError('행정안전부 목록 구조를 확인해 주세요: print_area 없음')
    entries, region = [], ''
    for node in area.children:
        if not isinstance(node, finder.Node):
            continue
        if node.tag == 'h3' and 'location_title' in node.attrs.get('class', '').split():
            link = next(node.walk('a'), None)
            if not link:
                continue
            region = finder.compact(link.text())
            entries.append({'name': region, 'region': region, 'level': '광역', 'url': clean_url(link.attrs.get('href', '')), 'aliases': [], 'source_url': SOURCE})
        elif node.tag == 'ul' and 'location_list' in node.attrs.get('class', '').split() and region:
            for link in node.walk('a'):
                name = finder.compact(link.text())
                if not re.fullmatch(r'[가-힣]+(?:시|군|구)', name):
                    continue
                entries.append({'name': f'{region} {name}', 'region': region, 'level': '행정시' if region == '제주특별자치도' else '시군구', 'url': clean_url(link.attrs.get('href', '')), 'aliases': aliases(region, name), 'source_url': SOURCE})
    if len(entries) < 200 or any(not e['url'] for e in entries):
        raise ValueError('목록이 불완전해 기존 파일을 유지합니다.')
    return entries


def homepage_links(page, url):
    links = list(page.links(url))
    # Some city footers expose related district sites through select options.
    for node in page.root.walk('option'):
        value = node.attrs.get('value', '')
        if value.startswith(('https://', 'http://')):
            links.append((node.text(), value, node))
    return links


def inspect_homepage(entry):
    entry = dict(entry)
    districts = []
    try:
        raw, final = finder.request(entry['url'])
        page = finder.Page(raw)
        for _ in range(2):
            redirect = finder.landing_redirect(page, final)
            if not redirect:
                break
            raw, final = finder.request(redirect)
            page = finder.Page(raw)
        local = entry['name'].split()[-1]
        identity_ok = finder.location_key(entry['name']) in finder.location_key(page.identity())
        # The directory independently ties this domain to this jurisdiction.
        # A unique city/county label may be shortened in its homepage header.
        if not identity_ok and entry['level'] == '시군구' and local.endswith(('시', '군')):
            identity_ok = finder.location_key(local) in finder.location_key(page.identity())
        if not identity_ok and entry['level'] == '일반구':
            identity_ok = finder.matching_identity(entry['name'], page, entry, final)
        entry['homepage_status'] = '기관명 확인' if identity_ok else '기관명 추가 확인'
        if identity_ok:
            entry['url'] = final
            host = urllib.parse.urlsplit(final).hostname
            links = homepage_links(page, final)
            organization = [(label, link) for label, link, _ in links if finder.compact(label) in ('조직도', '행정조직', '행정조직도', '조직안내', '조직및업무', '조직/업무') and urllib.parse.urlsplit(link).hostname == host and finder.within_district_scope(entry, link)]
            if organization:
                organization.sort(key=lambda item: 0 if finder.compact(item[0]) == '조직도' else 1)
                entry['org'] = clean_url(organization[0][1])
            if entry['level'] == '시군구' and local.endswith('시'):
                for label, link, _ in links:
                    district = re.fullmatch(r'([가-힣]{1,5}구)(?:청)?(?:홈페이지|누리집|바로가기)?', finder.compact(label))
                    if district and not district[1].endswith(('창구', '청구', '인구', '지구', '특구', '가구', '기구')) and finder.official_host(urllib.parse.urlsplit(link).hostname):
                        districts.append({'name': entry['name']+' '+district[1], 'region': entry['region'], 'level': '일반구', 'url': clean_url(link), 'aliases': [], 'source_url': final})
    except Exception:
        entry['homepage_status'] = '접속 추가 확인'
    entry['checked_at'] = finder.stamp()
    return entry, districts


def verify_district(entry):
    try:
        raw, final = finder.request(entry['url'])
        page = finder.Page(raw)
        for _ in range(2):
            redirect = finder.landing_redirect(page, final)
            if not redirect:
                break
            raw, final = finder.request(redirect)
            page = finder.Page(raw)
        city_district = finder.location_key(' '.join(entry['name'].split()[-2:]))
        if city_district not in finder.location_key(page.identity()):
            return None
        return {**entry, 'url': final, 'homepage_status': '기관명 확인', 'checked_at': finder.stamp()}
    except Exception:
        return None


def write_catalog(payload):
    DESTINATION.parent.mkdir(exist_ok=True)
    temporary = DESTINATION.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(DESTINATION)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory-only', action='store_true')
    args = parser.parse_args()
    raw, _ = finder.request(SOURCE)
    entries = parse_directory(raw)
    print('Official directory entries:', len(entries), flush=True)
    # Only exact hosts independently listed by MOIS become exceptions to the
    # general official-domain rule. Do not trust arbitrary .kr addresses.
    finder.CATALOG_HOSTS.update(urllib.parse.urlsplit(e['url']).hostname for e in entries)
    finder.CATALOG_HOSTS.update(h.removeprefix('www.') for h in list(finder.CATALOG_HOSTS))
    entries.extend(supplemental_entries(entries))
    finder.HINTS = entries
    payload = {'source_url': SOURCE, 'collected_at': finder.stamp(), 'websites': entries}
    if args.directory_only:
        write_catalog(payload)
        return
    checked, districts = [], []
    with ThreadPoolExecutor(max_workers=6) as workers:
        tasks = [workers.submit(inspect_homepage, e) for e in entries]
        for future in as_completed(tasks):
            entry, children = future.result()
            checked.append(entry)
            districts.extend(children)
            if len(checked) % 25 == 0:
                print('Homepages checked:', len(checked), '/', len(entries), flush=True)
    unique = {finder.location_key(e['name']): e for e in checked}
    verified_districts = []
    with ThreadPoolExecutor(max_workers=6) as workers:
        candidates = {finder.location_key(e['name']): e for e in districts}.values()
        for verified in workers.map(verify_district, candidates):
            if verified:
                unique.setdefault(finder.location_key(verified['name']), verified)
                verified_districts.append(verified)
    payload['websites'] = sorted(unique.values(), key=lambda e: (e['region'], e['name']))
    payload['homepage_checks'] = dict(collections.Counter(e.get('homepage_status') for e in payload['websites']))
    write_catalog(payload)
    print('Verified district links:', sorted({e['name'] for e in verified_districts}), flush=True)
    print('Total registered:', len(unique), 'Homepage checks:', payload['homepage_checks'], flush=True)


if __name__ == '__main__':
    main()
