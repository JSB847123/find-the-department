"""Read XLSX and CSV inputs without installing a spreadsheet runtime.

Only selected location columns are sent to the search engine. Uploaded bytes are
kept in memory, never saved. Formula cells use Excel's last saved cached value.
"""
import csv
import io
import posixpath
import zipfile
import xml.etree.ElementTree as ET

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MAX_ROWS = 10000
MAX_COLS = 150


def read_workbook(data, filename):
    if filename.lower().endswith((".csv", ".tsv")):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp949")
        dialect = csv.excel_tab if filename.lower().endswith(".tsv") else csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        if len(rows) > MAX_ROWS or any(len(r) > MAX_COLS for r in rows):
            raise ValueError("파일은 최대 10,000행, 150열까지 지원합니다.")
        return {"sheets": [{"name": "목록", "rows": rows}]}
    if not filename.lower().endswith(".xlsx"):
        raise ValueError(".xlsx, .csv, .tsv를 지원합니다. .xls는 Excel에서 .xlsx로 저장해 주세요.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 3000 or sum(i.file_size for i in archive.infolist()) > 60_000_000:
                raise ValueError("압축 해제 크기가 너무 큽니다. 지자체 목록만 새 파일로 저장해 주세요.")
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                strings = ["".join(n.itertext()) for n in root.findall("s:si", NS)]
            book = ET.fromstring(archive.read("xl/workbook.xml"))
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels if r.attrib.get("TargetMode") != "External"}
            sheets = []
            for sheet in book.findall("s:sheets/s:sheet", NS):
                target = targets.get(sheet.attrib.get(f"{{{REL}}}id"), "")
                path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
                if not path.startswith("xl/") or path not in archive.namelist():
                    continue
                root = ET.fromstring(archive.read(path))
                rows = []
                for row in root.findall("s:sheetData/s:row", NS):
                    row_index = int(row.attrib.get("r", len(rows) + 1)) - 1
                    if row_index >= MAX_ROWS:
                        raise ValueError("파일은 최대 10,000행까지 지원합니다.")
                    while len(rows) <= row_index:
                        rows.append([])
                    cells = rows[row_index]
                    for cell in row.findall("s:c", NS):
                        column = 0
                        for char in cell.attrib.get("r", "A1"):
                            if not char.isalpha():
                                break
                            column = column * 26 + ord(char.upper()) - 64
                        column -= 1
                        if column < 0 or column >= MAX_COLS:
                            raise ValueError("파일은 최대 150열까지 지원합니다.")
                        while len(cells) <= column:
                            cells.append("")
                        kind = cell.attrib.get("t", "")
                        value = cell.findtext("s:v", default="", namespaces=NS)
                        if kind == "s":
                            value = strings[int(value)] if value else ""
                        elif kind == "inlineStr":
                            value = "".join(n.text or "" for n in cell.findall("s:is//s:t", NS))
                        elif kind == "e":
                            value = ""  # Excel errors cannot become municipality names.
                        cells[column] = value
                sheets.append({"name": sheet.attrib["name"], "rows": rows})
            if not sheets:
                raise ValueError("읽을 수 있는 시트가 없습니다.")
            return {"sheets": sheets}
    except (zipfile.BadZipFile, ET.ParseError, KeyError, IndexError) as exc:
        raise ValueError("엑셀 파일을 읽을 수 없습니다. 암호를 해제하고 .xlsx로 다시 저장해 주세요.") from exc
