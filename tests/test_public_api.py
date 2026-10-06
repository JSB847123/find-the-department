import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from unittest.mock import patch
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import app
import finder
from public_api import OrgClient, parse_response, check_connection, SOURCE


def response(rows, total=None):
    return json.dumps({'StanOrgCd': [{'head': [{'totalCount': len(rows) if total is None else total}, {'RESULT': {'resultCode': 'INFO-0'}}]}, {'row': rows}]}, ensure_ascii=False)


def organization(code, full, low, parent='0000000', **extra):
    return dict(org_cd=code, full_nm=full, low_nm=low, high_cd=parent, stop_selt='0', **extra)


class PublicApiTests(unittest.TestCase):
    def test_encoded_and_decoded_keys_have_identical_request(self):
        key = 'sample+key/=='
        calls = []
        def request(url, **kwargs):
            calls.append(url)
            return response([]), url
        with patch('finder.request', side_effect=request):
            OrgClient(key).page(org_cd='1741000')
            OrgClient(urllib.parse.quote(key, safe='')).page(org_cd='1741000')
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)['ServiceKey'], [key])

    def test_json_xml_and_error_do_not_expose_server_message(self):
        row = organization('1000001', '서울특별시 중구 세무과', '세무과')
        self.assertEqual(parse_response(response([row]))[0], [row])
        xml = '<root><resultCode>INFO-0</resultCode><totalCount>1</totalCount><row><org_cd>1000001</org_cd><low_nm>세무과</low_nm></row></root>'
        self.assertEqual(parse_response(xml)[0][0]['low_nm'], '세무과')
        with self.assertRaises(ValueError) as error:
            parse_response('<OpenAPI_ServiceResponse><returnReasonCode>30</returnReasonCode><returnAuthMsg>secret-key</returnAuthMsg></OpenAPI_ServiceResponse>')
        self.assertNotIn('secret-key', str(error.exception))
        self.assertIn('인증', str(error.exception))

    def test_abandoned_organization_and_unknown_body_rejected(self):
        row = organization('1000001', '서울특별시 중구 세무과', '세무과')
        row['stop_selt'] = '1'
        self.assertEqual(parse_response(response([row]))[0], [])
        with self.assertRaises(ValueError):
            parse_response('{}')

    def test_scope_and_verified_parent_chain(self):
        rows = [organization('1000000', '서울특별시 중구', '중구'), organization('1000001', '서울특별시 중구 행정국', '행정국', '1000000'), organization('1000002', '서울특별시 중구 행정국 세무과', '세무과', '1000001'), organization('2000002', '부산광역시 중구 행정국 세무과', '세무과', '2000001')]
        with patch('finder.request', return_value=(response(rows), 'ignored')):
            candidates, _ = OrgClient('sample').candidates('서울 중구')
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['bureau'], '행정국')
        self.assertEqual(candidates[0]['org_code'], '1000002')
        self.assertFalse(candidates[0]['duty_verified'])
        self.assertTrue(all(e['url'] == SOURCE for e in candidates[0]['evidence']))

    def test_bureau_word_in_full_name_is_not_parent_proof(self):
        rows = [organization('1000002', '서울특별시 중구 행정국 세무과', '세무과', '0000000')]
        with patch('finder.request', return_value=(response(rows), 'ignored')):
            candidates, _ = OrgClient('sample').candidates('서울 중구')
        self.assertEqual(candidates[0]['bureau'], '')

    def test_bureau_is_sufficient_when_municipal_root_is_omitted(self):
        rows = [organization('1000001', '서울특별시 중구 행정국', '행정국', '1000000'), organization('1000002', '서울특별시 중구 행정국 세무과', '세무과', '1000001')]
        with patch('finder.request', return_value=(response(rows), 'ignored')) as network:
            candidates, notes = OrgClient('sample').candidates('서울 중구')
        self.assertEqual(network.call_count, 1)
        self.assertEqual(candidates[0]['bureau'], '행정국')
        self.assertEqual(notes, [])

    def test_parent_lookup_cycle_terminates(self):
        row = organization('1000002', '서울특별시 중구 세무과', '세무과', '1000001')
        parent = organization('1000001', '서울특별시 중구 행정국', '행정국', '1000002')
        with patch('finder.request', side_effect=[(response([row]), ''), (response([parent]), '')]) as network:
            candidates, _ = OrgClient('sample').candidates('서울 중구')
        self.assertEqual(network.call_count, 2)
        self.assertEqual(candidates[0]['bureau'], '행정국')

    def test_pagination_collects_second_page_and_stops_repeat(self):
        first = [organization(f'{1000000+i:07d}', '서울특별시 중구 기타', '기타') for i in range(100)]
        last = organization('1000100', '서울특별시 중구 세무과', '세무과')
        with patch('finder.request', side_effect=[(response(first, 101), ''), (response([last], 101), '')]) as network:
            candidates, _ = OrgClient('sample').candidates('서울 중구')
        self.assertEqual(network.call_count, 2)
        self.assertEqual(candidates[0]['org_code'], '1000100')
        with patch('finder.request', return_value=(response(first, 1000), '')) as network:
            _, notes = OrgClient('sample').candidates('서울 중구')
        self.assertEqual(network.call_count, 2)
        self.assertTrue(any('반복' in n for n in notes))

    def test_public_only_result_does_not_claim_tax_duty(self):
        rows = [organization('1000002', '서울특별시 중구 세무과', '세무과')]
        with patch('finder.request', return_value=(response(rows), 'ignored')):
            result = finder.find_department('서울 중구', public_key='sample')
        self.assertEqual(result['status'], '담당업무 미확인')
        self.assertEqual(finder.recommended_candidate(result), {})
        self.assertFalse(result['confirmed'])
        self.assertFalse(result['candidates'][0]['duty_verified'])
        self.assertNotIn('sample', json.dumps(result, ensure_ascii=False))

    def test_official_duty_merges_code_and_detects_bureau_conflict(self):
        rows = [organization('1000000', '서울특별시 중구', '중구'), organization('1000001', '서울특별시 중구 재정국', '재정국', '1000000'), organization('1000002', '서울특별시 중구 재정국 세무과', '세무과', '1000001')]
        page = '<title>서울특별시 중구청</title><main><h2>세무과</h2><p>지방소득세 부과</p><ul><li><a href="/org">행정국</a><ul><li><a href="/tax">세무과</a></li></ul></li></ul></main>'
        def request(url, **kwargs):
            return (response(rows), '') if kwargs.get('api') else (page, url)
        with patch('finder.request', side_effect=request):
            result = finder.find_department('서울 중구', source_url='https://city.go.kr/tax', public_key='sample')
        self.assertEqual(result['status'], '후보 발견')
        self.assertEqual(result['candidates'][0]['bureau'], '')
        self.assertEqual(result['candidates'][0]['org_code'], '1000002')
        self.assertTrue(any('다릅니다' in n for n in result['notes']))

    def test_connection_requires_expected_org_code(self):
        row = organization('1741000', '행정안전부', '행정안전부')
        with patch('finder.request', return_value=(response([row]), 'ignored')):
            self.assertIn('연결 성공', check_connection('sample'))
        with patch('finder.request', return_value=(response([]), 'ignored')):
            with self.assertRaises(ValueError):
                check_connection('sample')

    def test_city_page_does_not_merge_a_district_department(self):
        row = organization('1000002', '경기도 수원시 장안구 세무과', '세무과')
        page = '<title>경기도 수원시</title><main><h2>세무과</h2><p>지방소득세 부과</p></main>'
        def request(url, **kwargs):
            return (response([row]), '') if kwargs.get('api') else (page, url)
        with patch('finder.request', side_effect=request):
            result = finder.find_department('경기도 수원시', source_url='https://city.go.kr/tax', public_key='sample')
        self.assertNotIn('org_code', result['candidates'][0])
        self.assertEqual(len(result['candidates']), 2)

    def test_organization_response_order_never_selects_finance_for_export(self):
        rows = [organization('1000000', '서울특별시 중구', '중구'),
                organization('1000001', '서울특별시 중구 재무과', '재무과', '1000000'),
                organization('1000002', '서울특별시 중구 세무1과', '세무1과', '1000000'),
                organization('1000003', '서울특별시 중구 세무2과', '세무2과', '1000000')]
        with patch('finder.request', return_value=(response(rows), 'ignored')):
            result = finder.find_department('서울 중구', public_key='sample')
        self.assertEqual(result['candidates'][0]['department'], '재무과')
        self.assertEqual(finder.recommended_candidate(result), {})
        import csv
        import io
        exported = list(csv.DictReader(io.StringIO(app.csv_bytes([result]).decode('utf-8-sig'))))[0]
        for column in ('상위 국', '담당 부서', '팀', '전화번호', '공문 수신처', '기관코드'):
            self.assertEqual(exported[column], '')
        self.assertIn('세무2과', exported['조직 후보 목록(담당업무 미확인)'])
        self.assertEqual(exported['상태'], '담당업무 미확인')
        # Existing saved sessions with the old status obey the same rule.
        result['status'] = '조직 후보'
        self.assertEqual(finder.recommended_candidate(result), {})

    def test_bupyeong_follows_live_department_links_instead_of_fixed_answer(self):
        root = 'https://www.icbp.go.kr/main/introduction/guidance/organization.jsp'
        base = 'https://www.icbp.go.kr/main/organization/organizationInfoList.do?orgno0=1&orgno1='
        organization_page = '<title>인천광역시 부평구 조직도</title><main><ul><li><a href="#">기획문화국</a><ul><li><a href="'+base+'96">재무과</a></li><li><a href="'+base+'111">세무1과</a></li><li><a href="'+base+'129">세무2과</a></li></ul></li></ul></main>'
        rows = [organization('3540000', '인천광역시 부평구', '부평구'), organization('3540180', '인천광역시 부평구 기획문화국', '기획문화국', '3540000'), organization('3540184', '인천광역시 부평구 기획문화국 재무과', '재무과', '3540180'), organization('3540185', '인천광역시 부평구 기획문화국 세무1과', '세무1과', '3540180'), organization('3540186', '인천광역시 부평구 기획문화국 세무2과', '세무2과', '3540180')]
        for duty_department, duty_id in [('세무2과', '129'), ('세무1과', '111')]:
            with self.subTest(duty_department=duty_department):
                pages = {root: organization_page}
                for dept, number in [('재무과','96'),('세무1과','111'),('세무2과','129')]:
                    duty = '<div class="para_line"><p class="bl02">'+dept+' &gt; 지방소득세팀</p></div><div class="para_line"><div class="tableBox"><table><tr><td>팀장</td><td>032-509-6281</td><td>지방소득세팀 업무총괄</td></tr><tr><td>주무관</td><td>032-509-6287</td><td>법인지방소득세 부과 및 징수</td></tr></table></div></div>' if number == duty_id else '<p>다른 업무</p>'
                    pages[base+number] = '<title>인천광역시 부평구 '+dept+'</title><div id="detail_con">'+duty+'</div>'
                def request(url, **kwargs):
                    return (response(rows), '') if kwargs.get('api') else (pages[url], url)
                with patch('finder.request', side_effect=request):
                    result = finder.find_department('인천 부평', public_key='sample')
                recommended = finder.recommended_candidate(result)
                self.assertEqual(recommended['department'], duty_department)
                self.assertEqual(recommended['bureau'], '기획문화국')
                self.assertEqual(recommended['team'], '지방소득세팀')
                self.assertEqual(recommended['phone'], '032-509-6281')
                self.assertTrue(recommended['duty_verified'])
                self.assertTrue(all(c['department'] == duty_department for c in result['candidates'] if finder.has_duty_evidence(c)))

    def test_saving_one_provider_preserves_other_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'ROOT', Path(directory)):
            app.save_settings({'NAVER_CLIENT_ID': 'sample-id', 'NAVER_CLIENT_SECRET': 'sample-secret'})
            app.save_settings({'PUBLIC_DATA_SERVICE_KEY': 'first'})
            app.save_settings({'PUBLIC_DATA_SERVICE_KEY': 'second'})
            values = app.env_values()
            self.assertEqual(values['NAVER_CLIENT_ID'], 'sample-id')
            self.assertEqual(values['NAVER_CLIENT_SECRET'], 'sample-secret')
            self.assertEqual(values['PUBLIC_DATA_SERVICE_KEY'], 'second')


class SettingsHttpTests(unittest.TestCase):
    def test_settings_save_and_check_keep_key_off_responses(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'ROOT', Path(directory)), patch.dict(app.os.environ, {}, clear=True):
            (Path(directory) / 'static').mkdir()
            (Path(directory) / 'static' / 'index.html').write_text('test', encoding='utf-8')
            server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
            with patch.object(app, 'PORT', server.server_port):
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                url = f'http://127.0.0.1:{server.server_port}'
                client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
                try:
                    client.open(url + '/').close()
                    def post(path, payload):
                        req = urllib.request.Request(url + path, json.dumps(payload).encode(), headers={'Origin': url, 'Content-Type': 'application/json'})
                        with client.open(req) as result:
                            return json.loads(result.read())
                    key = 'test-only-key+/'
                    answer = post('/api/settings', {'provider': 'public', 'service_key': key})
                    self.assertTrue(answer['public_configured'])
                    with client.open(url + '/api/settings') as result:
                        settings = result.read().decode()
                    self.assertNotIn(key, settings)
                    with patch('public_api.check_connection', return_value='연결 성공') as check:
                        self.assertTrue(post('/api/settings/test', {})['ok'])
                        check.assert_called_once_with(key)
                    self.assertNotIn(key, json.dumps(answer))
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        post('/api/settings', {'provider': 'public', 'service_key': 'bad\nkey'})
                    self.assertEqual(error.exception.code, 400)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()


if __name__ == '__main__':
    unittest.main()
