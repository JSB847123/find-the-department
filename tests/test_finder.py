import base64
import csv
import io
import json
import unittest
import zipfile
from unittest.mock import patch

import finder
from app import csv_bytes
from workbook import read_workbook


class EvidenceTests(unittest.TestCase):
    def test_hierarchy_requires_nested_relation(self):
        page = finder.Page('<ul><li><a href="/a">행정국</a><ul><li><a href="/b">세무1과</a></li></ul></li><li><a href="/c">복지국</a><ul><li><a href="/d">재무과</a></li></ul></li></ul>')
        mapping = finder.hierarchy(page, 'https://city.go.kr/org')
        self.assertEqual(mapping['세무1과'][0]['bureau'], '행정국')
        self.assertEqual(mapping['재무과'][0]['bureau'], '복지국')
        unrelated = finder.Page('<div><a href="/a">행정국</a></div><div><a href="/b">세무1과</a></div>')
        self.assertNotIn('세무1과', finder.hierarchy(unrelated, 'https://city.go.kr/'))
        flat = finder.Page('<div><a href="/a">행정국</a><div><a href="/b">세무1과</a></div></div>')
        self.assertNotIn('세무1과', finder.hierarchy(flat, 'https://city.go.kr/'))

    def test_sidebar_department_is_not_duty_evidence(self):
        page = finder.Page('<nav><a>세무1과</a></nav><main><table><tr><td>주무관</td><td>031-123-1234</td><td>법인지방소득세 부과</td></tr></table></main>')
        self.assertEqual(finder.extract_candidates(page, 'https://city.go.kr/'), [])

    def test_duties_and_phone_are_kept_in_same_row(self):
        page = finder.Page('<main><h2>세무1과</h2><div><h3>지방소득세팀</h3><table><tr><td>주무관</td><td>062-360-7827</td><td>법인지방소득세 부과징수</td></tr><tr><td>주무관</td><td>062-360-7607</td><td>지방소득세 특별징수</td></tr></table></div></main>')
        rows = finder.extract_candidates(page, 'https://city.go.kr/')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['phone'], '062-360-7827')
        self.assertEqual(rows[0]['team'], '지방소득세팀')
        self.assertEqual(rows[0]['duty'], '법인지방소득세 부과징수')

    def test_wrong_institution_is_rejected(self):
        page = finder.Page('<title>부산광역시 서구청</title><main><h2>세무과</h2><p>지방소득세 부과</p></main>')
        self.assertFalse(finder.matching_identity('대전광역시 서구', page))
        self.assertTrue(finder.matching_identity('부산 서구', page))

    def test_ambiguous_region_does_not_search(self):
        with patch('finder.request') as network:
            result = finder.find_department('서구', ('id', 'secret'))
        network.assert_not_called()
        self.assertEqual(result['candidates'], [])

    def test_synthetic_discovery_does_not_guess_bureau(self):
        page = '<title>서울특별시 강남구청</title><main><h2>세무1과</h2><p>지방소득세 세입 이체 02-123-4567</p></main>'
        with patch('finder.request', return_value=(page, 'https://www.gangnam.go.kr/tax')):
            result = finder.find_department('서울 강남구', source_url='https://www.gangnam.go.kr/tax')
        self.assertEqual(result['candidates'][0]['department'], '세무1과')
        self.assertEqual(result['candidates'][0]['bureau'], '')
        self.assertFalse(result['confirmed'])

    def test_private_address_and_fake_domain_are_rejected(self):
        with self.assertRaises(ValueError):
            finder.validate_url('https://city.go.kr.evil.example')
        with patch('finder.socket.getaddrinfo', return_value=[(0, 0, 0, '', ('127.0.0.1', 443))]):
            with self.assertRaises(ValueError):
                finder.validate_url('https://city.go.kr/')

    def test_wrapped_staff_tables_keep_department_and_team_context(self):
        page = finder.Page('<title>시청 직원안내</title><main><div class="para_line"><p>세무1과 &gt; 법인지방소득세팀</p></div><div class="para_line"><div class="tableBox"><table><tr><td>주무관</td><td>031-123-1001</td><td>법인지방소득세 부과</td></tr></table></div></div><div class="para_line"><p>세무2과 &gt; 개인지방소득세팀</p></div><div class="para_line"><div class="tableBox"><table><tr><td>주무관</td><td>031-123-1002</td><td>개인지방소득세 부과</td></tr></table></div></div></main>')
        rows = finder.extract_candidates(page, 'https://city.go.kr/staff')
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]['department'], rows[0]['team'], rows[0]['phone']), ('세무1과', '법인지방소득세팀', '031-123-1001'))
        self.assertEqual((rows[1]['department'], rows[1]['team'], rows[1]['phone']), ('세무2과', '개인지방소득세팀', '031-123-1002'))

    def test_team_heading_is_not_duty_and_does_not_cross_another_table(self):
        page = finder.Page('<title>시청 세무과</title><main><h3>지방소득세팀</h3><table><tr><td>팀장</td><td>031-123-1001</td><td>지방소득세팀 업무총괄</td></tr></table><table><tr><td>주무관</td><td>031-123-1002</td><td>지방소득세 자료 관리</td></tr></table></main>')
        rows = finder.extract_candidates(page, 'https://city.go.kr/staff')
        self.assertEqual(rows[0]['team'], '지방소득세팀')
        self.assertEqual(rows[1]['team'], '')
        self.assertGreater(rows[0]['score'], rows[1]['score'])

    def test_legacy_duty_evidence_and_explicit_confirmation_remain_supported(self):
        legacy = {'department': '세무과', 'duty': '지방소득세 부과', 'evidence': [{'type':'업무안내','text':'지방소득세 부과'}]}
        organization = {'department':'재무과', 'duty_verified':False, 'duty':'지방소득세 담당업무 미확인', 'evidence':[{'type':'기관코드 API','text':'재무과'}]}
        self.assertIs(finder.recommended_candidate({'candidates':[organization, legacy]}), legacy)
        self.assertIs(finder.recommended_candidate({'candidates':[organization], 'confirmed':True, 'selected':organization}), organization)


class WorkbookTests(unittest.TestCase):
    def make_book(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            z.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="목록" r:id="rId1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr('xl/sharedStrings.xml', '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>시·도</t></si><si><r><t>전남광주</t></r><r><t>통합특별시</t></r></si></sst>')
            z.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>지자체</t></is></c></row><row r="3"><c r="A3" t="s"><v>1</v></c><c r="C3" t="str"><f>"서구"</f><v>서구</v></c></row></sheetData></worksheet>')
        return stream.getvalue()

    def test_xlsx_preserves_sparse_columns_rows_and_cached_formula(self):
        sheets = read_workbook(self.make_book(), '기관.xlsx')['sheets']
        self.assertEqual(sheets[0]['name'], '목록')
        self.assertEqual(sheets[0]['rows'], [['시·도', '', '지자체'], [], ['전남광주통합특별시', '', '서구']])

    def test_cp949_and_csv_quoted_cells(self):
        data = '지자체,메모\r\n서울특별시 강남구,"확인, 필요"\r\n'.encode('cp949')
        self.assertEqual(read_workbook(data, '목록.csv')['sheets'][0]['rows'][1][1], '확인, 필요')

    def test_old_xls_is_rejected_clearly(self):
        with self.assertRaisesRegex(ValueError, 'xlsx'):
            read_workbook(b'old', '목록.xls')

    def test_export_escapes_formula_and_has_no_unconfirmed_recipient(self):
        rows = [{"location": '=HYPERLINK("bad")', 'canonical':'강남구', 'status':'후보 발견', 'confirmed':False, 'candidates':[{'department':'세무과', 'phone':'02-123-4567'}], 'notes':[], 'recipient':'미확인 수신처'}]
        output = csv_bytes(rows).decode('utf-8-sig')
        values = list(csv.reader(io.StringIO(output)))
        self.assertTrue(values[1][0].startswith("'="))
        self.assertEqual(values[1][7], '')


if __name__ == '__main__':
    unittest.main()
