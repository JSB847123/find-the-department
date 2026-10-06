import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import finder
from scripts import update_website_catalog as catalog_builder


class WebsiteDiscoveryTests(unittest.TestCase):
    HOME = 'https://city.go.kr/'
    ORG = HOME + 'org'
    STAFF = HOME + 'staff'
    PAGES = {
        HOME: '<title>서울특별시 중구청</title><nav><a href="/org">조직도</a></nav>',
        ORG: '<title>서울특별시 중구청 조직도</title><main><ul><li><a href="#">행정국</a><ul><li><a href="/staff">세무과</a></li></ul></li></ul></main>',
        STAFF: '<title>서울특별시 중구청 세무과</title><main><table><tr><td>주무관</td><td>02-123-4567</td><td>개인·법인 지방소득세 세입 이체</td></tr></table></main>',
    }

    def test_homepage_follows_organization_and_staff_without_keys(self):
        with patch('finder.request', side_effect=lambda url, **kw: (self.PAGES[url], url)):
            row = finder.find_department('서울 중구', source_url=self.HOME)
        candidate = finder.recommended_candidate(row)
        self.assertEqual((candidate['bureau'], candidate['department']), ('행정국', '세무과'))
        self.assertEqual(row['website_source'], self.HOME)
        self.assertIn('조직도', next(c for c in row['website_checks'] if c['url'] == self.ORG)['roles'])
        self.assertIn('업무안내', next(c for c in row['website_checks'] if c['url'] == self.STAFF)['roles'])
        self.assertFalse(row['confirmed'])

    def test_complete_website_evidence_avoids_search_and_precedes_org_api(self):
        calls = []
        def request(url, **kw):
            calls.append('website')
            return self.PAGES[url], url
        def org(location):
            calls.append('api')
            return [], []
        with patch('finder.request', side_effect=request), patch('finder.search') as search, patch('public_api.OrgClient.candidates', side_effect=org):
            row = finder.find_department('서울 중구', ('id', 'secret'), source_url=self.HOME, public_key='sample')
        search.assert_not_called()
        self.assertEqual(calls[-1], 'api')
        self.assertEqual(row['status'], '후보 발견')

    def test_search_supplements_missing_staff_evidence(self):
        pages = {**self.PAGES, self.STAFF: '<title>서울특별시 중구청</title><p>직원표 준비 중</p>', self.HOME+'tax': self.PAGES[self.STAFF]}
        with patch('finder.request', side_effect=lambda url, **kw: (pages[url], url)), patch('finder.search', return_value=[{'url': self.HOME+'tax'}]) as search:
            row = finder.find_department('서울 중구', ('id', 'secret'), source_url=self.HOME)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(finder.recommended_candidate(row)['department'], '세무과')

    def test_organization_alone_remains_unverified_and_exports_its_source(self):
        pages = {**self.PAGES, self.STAFF: '<title>서울특별시 중구청</title><p>동적 직원 검색</p>'}
        with patch('finder.request', side_effect=lambda url, **kw: (pages[url], url)):
            row = finder.find_department('서울 중구', source_url=self.HOME)
        self.assertEqual(row['status'], '담당업무 미확인')
        self.assertEqual(finder.recommended_candidate(row), {})
        self.assertEqual(row['candidates'][0]['evidence'][0]['type'], '조직도')
        exported = list(csv.DictReader(io.StringIO(app.csv_bytes([row]).decode('utf-8-sig'))))[0]
        self.assertEqual(exported['담당 부서'], '')
        self.assertEqual(exported['조직도 URL'], self.ORG)
        self.assertEqual(exported['홈페이지 시작 URL'], self.HOME)
        self.assertIn('세무과', exported['조직 후보 목록(담당업무 미확인)'])
        self.assertIn(self.ORG, exported['홈페이지 조회 기록'])

    def test_wrong_municipality_and_failed_page_are_recorded(self):
        with patch('finder.request', return_value=('<title>부산광역시 중구청</title>', self.HOME)):
            row = finder.find_department('서울 중구', source_url=self.HOME)
        self.assertEqual(row['website_checks'][0]['status'], '기관 불일치')
        self.assertEqual(row['candidates'], [])
        with patch('finder.request', side_effect=ValueError('HTML 업무안내 페이지만 자동 분석합니다.')):
            row = finder.find_department('서울 중구', source_url=self.HOME)
        self.assertEqual(row['website_checks'][0]['status'], '조회 실패')
        self.assertIn('HTML', row['website_checks'][0]['message'])

    def test_dynamic_gangnam_links_only_use_explicit_department_ids(self):
        page = finder.Page('<title>서울특별시 강남구청</title><main><ul><li><p><a href="javascript:void(0);">기획경제국</a></p><ul><li><a href="javascript:void(0);" onclick="ajaxRequest(\'3220267\',\'\')">지방소득세과</a></li></ul></li></ul></main>')
        mapping = finder.hierarchy(page, 'https://www.gangnam.go.kr/org')
        self.assertEqual(mapping['지방소득세과'][0]['bureau'], '기획경제국')
        self.assertEqual(mapping['지방소득세과'][0]['department_url'], 'https://www.gangnam.go.kr/dept/info/view.do?ndi_dept_id=3220267')
        self.assertEqual(finder.hierarchy(page, 'https://other.go.kr/org'), {})
        staff = finder.Page('<main><h2>지방소득세과</h2><table><tr><td>지방소득세1팀</td><td>팀장</td><td>02-123-4567</td><td>지방소득세1팀 업무총괄</td></tr></table></main>')
        candidate = finder.extract_candidates(staff, 'https://www.gangnam.go.kr/staff')[0]
        self.assertEqual(candidate['team'], '지방소득세1팀')


class WebsiteRegistryTests(unittest.TestCase):
    def test_registry_persists_normalized_name_and_builtin_override(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'DATA', Path(folder)):
            app.save_website('  인천   부평구 ', 'https://www.icbp.go.kr/#top')
            entries = app.website_sources()
            bupyeong = [e for e in entries if e['location'] == '인천광역시 부평구']
            self.assertEqual(len(bupyeong), 1)
            self.assertEqual(bupyeong[0]['url'], 'https://www.icbp.go.kr/')
            self.assertFalse(bupyeong[0]['builtin'])
            self.assertEqual(len(json.loads((Path(folder)/'websites.json').read_text(encoding='utf-8'))), 1)

    def test_unsafe_or_ambiguous_registration_is_rejected(self):
        for location, url in [('서구', 'https://city.go.kr/'), ('서울 중구', 'http://127.0.0.1/'), ('서울 중구', 'https://city.go.kr.evil.example/'), ('서울 중구', 'https://user:pass@city.go.kr/'), ('서울 중구', 'https://city.go.kr:8765/')]:
            with self.subTest(location=location, url=url), self.assertRaises(ValueError):
                app.save_website(location, url)

    def test_registered_site_reused_and_explicit_input_overrides_it(self):
        for override in ('', 'https://city.go.kr/new-org'):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as folder, patch.object(app, 'DATA', Path(folder)), patch.object(app, 'JOBS', {}), patch('app.credentials', return_value=('', '')), patch('app.public_key', return_value=''):
                app.save_website('서울특별시 중구', 'https://city.go.kr/org')
                app.JOBS['test'] = {'cancelled': False, 'results': []}
                sources = {'서울 중구': override} if override else {}
                with patch('app.find_department', return_value={'location': '서울 중구'}) as find:
                    app.run_job('test', ['서울 중구'], 'all', sources)
                self.assertEqual(find.call_args.args[3], override or 'https://city.go.kr/org')
                self.assertEqual(app.JOBS['test']['state'], 'done')


class NationalCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((Path(__file__).resolve().parents[1]/'catalog'/'websites.json').read_text(encoding='utf-8'))
        cls.entries = cls.catalog['websites']

    def test_broad_coverage_has_unique_names_and_public_source_urls(self):
        self.assertGreaterEqual(len(self.entries), 284)
        self.assertEqual(len({finder.location_key(e['name']) for e in self.entries}), len(self.entries))
        self.assertEqual(len({e['region'] for e in self.entries}), 16)
        for entry in self.entries:
            with self.subTest(name=entry['name']):
                self.assertTrue(catalog_builder.clean_url(entry['url']))
                self.assertTrue(catalog_builder.clean_url(entry['source_url']))
                self.assertTrue(finder.official_host(finder.urllib.parse.urlsplit(entry['url']).hostname))
                if 'homepage_status' in entry:  # --directory-only skips checks.
                    self.assertIn(entry['homepage_status'], ('기관명 확인', '기관명 추가 확인', '접속 추가 확인'))
                    self.assertIn('checked_at', entry)
                if entry.get('org'):
                    self.assertTrue(catalog_builder.clean_url(entry['org']))

    def test_ambiguous_districts_and_counties_keep_separate_registrations(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app, 'DATA', Path(folder)):
            sources = {e['location']:e['url'] for e in app.website_sources()}
        for left, right in [('서울특별시 중구','부산광역시 중구'), ('대전광역시 서구','부산광역시 서구'), ('강원특별자치도 고성군','경상남도 고성군')]:
            self.assertIn(left, sources)
            self.assertIn(right, sources)
            self.assertNotEqual(sources[left], sources[right])
        self.assertEqual(app.website_location('제주도 서귀포시'), '제주특별자치도 서귀포시')
        self.assertEqual(app.website_location('광주 북구'), '전남광주통합특별시 북구')
        self.assertEqual(app.website_location('전남 나주시'), '전남광주통합특별시 나주시')
        self.assertEqual(app.website_location('경기 수원특례시 장안구'), '경기도 수원시 장안구')

    def test_general_district_presets_have_parent_city_and_survive_refresh(self):
        entries = catalog_builder.supplemental_entries(self.entries)
        self.assertEqual(len(entries), 39)
        names = {e['name'] for e in entries}
        for name in ['경기도 수원시 장안구', '경기도 성남시 분당구', '경기도 화성시 만세구', '경상북도 포항시 남구', '경상남도 창원시 마산합포구']:
            self.assertIn(name, names)

    def test_nonstandard_official_domains_require_exact_catalog_host(self):
        for host in ('www.nowon.kr','nowon.kr','www.donggu.kr','www.suseong.kr'):
            self.assertTrue(finder.official_host(host))
        for host in ('unlisted.kr','evil.nowon.kr','www.nowon.kr.evil.example'):
            self.assertFalse(finder.official_host(host))

    def test_short_city_header_only_matches_the_registered_domain(self):
        hint = next(h for h in finder.HINTS if h['name'] == '경기도 의정부시')
        page = finder.Page('<title>의정부시청</title>')
        self.assertTrue(finder.matching_identity(hint['name'], page, hint, hint['url']))
        self.assertFalse(finder.matching_identity(hint['name'], page, hint, 'https://different.go.kr/'))
        district = next(h for h in finder.HINTS if h['name'] == '경기도 고양시 덕양구')
        self.assertTrue(finder.matching_identity(district['name'], finder.Page('<title>덕양구청</title>'), district, district['url']))

    def test_shared_city_portals_do_not_cross_district_or_city_paths(self):
        cases = [
            ('경기도 안산시 단원구', 'https://www.ansan.go.kr/danwongu/common/staff', 'https://www.ansan.go.kr/sangnokgu/common/staff'),
            ('경기도 고양시 덕양구', 'https://www.goyang.go.kr/dygu/staff', 'https://www.goyang.go.kr/www/staff'),
            ('충청남도 천안시 동남구', 'https://www.cheonan.go.kr/dongnam/staff', 'https://www.cheonan.go.kr/seobuk/staff'),
            ('경상남도 창원시 의창구', 'https://www.changwon.go.kr/cwportal/gu/11094/staff.web', 'https://www.changwon.go.kr/cwportal/gu/11098/staff.web'),
            ('경상북도 포항시 북구', 'https://www.pohang.go.kr/gu/staff/list.do?deptCode=5040044&mid=0203010000', 'https://www.pohang.go.kr/gu/staff/list.do?mid=0103010000'),
        ]
        for name, correct, wrong in cases:
            hint = next(h for h in finder.HINTS if h['name'] == name)
            with self.subTest(name=name):
                self.assertTrue(finder.within_district_scope(hint, correct))
                self.assertFalse(finder.within_district_scope(hint, wrong))
                self.assertFalse(finder.matching_identity(name, finder.Page(f'<title>{name}</title>'), hint, wrong))
        for hint in finder.HINTS:
            if hint.get('level') == '일반구':
                self.assertTrue(finder.within_district_scope(hint, hint['org']), hint['name'])

    def test_crawler_follows_district_staff_and_skips_sibling_navigation(self):
        hint = next(h for h in finder.HINTS if h['name'] == '경기도 고양시 덕양구')
        staff = 'https://www.goyang.go.kr/dygu/staff'
        wrong = 'https://www.goyang.go.kr/ilsegu/staff'
        pages = {hint['url']:f'<title>덕양구청</title><a href="{staff}">직원안내</a><a href="{wrong}">직원검색</a>', staff:'<main><h2>세무과</h2><table><tr><td>031-123-1001</td><td>지방소득세 세입 이체</td></tr></table></main>'}
        with patch('finder.request', side_effect=lambda url, **kw:(pages[url], url)) as request:
            row = finder.find_department('경기 고양시 덕양구', source_url=hint['url'])
        self.assertEqual(finder.recommended_candidate(row)['department'], '세무과')
        self.assertNotIn(wrong, [call.args[0] for call in request.call_args_list])

    def test_directory_parser_excludes_unrelated_footer_and_fails_closed(self):
        links = ''.join(f'<li><a href="https://city{i}.go.kr/">경{chr(0xac00+i)}시</a></li>' for i in range(200))
        raw = f'<div id="print_area"><h3 class="location_title"><a href="https://province.go.kr/">전남광주통합특별시</a></h3><ul class="location_list">{links}</ul></div><footer><a href="https://wrong.go.kr/">다른시</a></footer>'
        entries = catalog_builder.parse_directory(raw)
        self.assertEqual(len(entries), 201)
        self.assertEqual(entries[1]['aliases'][0], '전라남도 경가시')
        self.assertFalse(any(e['url']=='https://wrong.go.kr/' for e in entries))
        with self.assertRaises(ValueError):
            catalog_builder.parse_directory('<p>사이트 점검 중</p>')

    def test_catalog_urls_discard_ephemeral_parameters_and_fragments(self):
        self.assertEqual(catalog_builder.clean_url('https://city.go.kr/staff?mid=0102010000&token=123#top'), 'https://city.go.kr/staff?mid=0102010000')
        self.assertEqual(catalog_builder.clean_url('https://city.go.kr/contents?key=7&name=%ED%85%8C%EC%8A%A4%ED%8A%B8'), 'https://city.go.kr/contents?key=7&name=%ED%85%8C%EC%8A%A4%ED%8A%B8')


class LandingPageTests(unittest.TestCase):
    def test_literal_same_host_redirects_and_conditional_script_rejection(self):
        for raw in ['<meta http-equiv="refresh" content="0; URL=/main">', '<script>window.location.href="/main";</script>']:
            self.assertEqual(finder.landing_redirect(finder.Page(raw),'https://city.go.kr/'), 'https://city.go.kr/main')
        for raw in ['<script>if (check) location.href="/main";</script>', '<meta http-equiv="refresh" content="0; url=https://other.go.kr/main">', '<script>location.href="/";</script>']:
            self.assertEqual(finder.landing_redirect(finder.Page(raw),'https://city.go.kr/'), '')

    def test_landing_redirect_is_recorded_before_staff_page(self):
        pages = {'https://city.go.kr/':'<meta http-equiv="refresh" content="0;url=/staff">', 'https://city.go.kr/staff':'<title>서울특별시 중구청</title><main><h2>세무과</h2><p>지방소득세 세입 이체</p></main>'}
        with patch('finder.request', side_effect=lambda url, **kw:(pages[url],url)):
            row = finder.find_department('서울 중구', source_url='https://city.go.kr/')
        self.assertEqual(row['website_checks'][0]['status'], '안내 페이지')
        self.assertEqual(finder.recommended_candidate(row)['department'], '세무과')


if __name__ == '__main__':
    unittest.main()
