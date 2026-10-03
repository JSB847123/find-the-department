"""Local web app. Run: python app.py --open"""
import argparse
import base64
import copy
import csv
import io
import json
import os
import re
import secrets
import threading
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from finder import find_department, stamp, location_key
from workbook import read_workbook

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOCK = threading.RLock()
JOBS = {}
WORKERS = ThreadPoolExecutor(max_workers=1)
SESSION = secrets.token_urlsafe(32)
PORT = 8765
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}


def env_values():
    values = {}
    path = ROOT / ".env.local"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def credentials():
    values = env_values()
    return (os.environ.get("NAVER_CLIENT_ID", "") or values.get("NAVER_CLIENT_ID", ""), os.environ.get("NAVER_CLIENT_SECRET", "") or values.get("NAVER_CLIENT_SECRET", ""))


def public_key():
    return os.environ.get("PUBLIC_DATA_SERVICE_KEY", "") or env_values().get("PUBLIC_DATA_SERVICE_KEY", "")


def save_settings(updates):
    path = ROOT / ".env.local"
    with LOCK:
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
        lines = [line for line in lines if line.split("=", 1)[0].strip() not in updates]
        lines.extend(f"{key}={value}" for key, value in updates.items())
        temporary = ROOT / ".env.local.tmp"
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(path)


def save_json(path, value):
    DATA.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def saved_results():
    path = DATA / "confirmed.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []


def run_job(job_id, locations, kind, sources):
    creds = credentials()
    org_key = public_key()
    for index, location in enumerate(locations):
        with LOCK:
            if JOBS[job_id]["cancelled"]:
                break
            JOBS[job_id]["current"] = location
            JOBS[job_id]["state"] = "running"
        try:
            row = find_department(location, creds, kind, sources.get(location, ""), public_key=org_key)
        except Exception:
            row = {"location": location, "canonical": location, "status": "검토 필요", "candidates": [], "notes": ["조회 중 오류가 발생했습니다. 공식 홈페이지 링크로 다시 확인해 주세요."], "checked_at": stamp(), "confirmed": False}
        row["id"] = secrets.token_hex(8)
        with LOCK:
            JOBS[job_id]["results"].append(row)
            JOBS[job_id]["completed"] = index + 1
    with LOCK:
        job = JOBS[job_id]
        job["state"] = "cancelled" if job["cancelled"] else "done"
        job["current"] = ""
        save_json(DATA / "last-session.json", job)


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["입력 지자체", "확인 기관명", "상위 국", "담당 부서", "팀", "전화번호", "담당업무", "공문 수신처", "상태", "업무 근거 URL", "조직도 URL", "조회 시각", "사용자 확인 시각", "검토 메모", "기관코드", "기관코드 전체명", "기관코드 자료 URL"])
    for row in rows:
        selected = row.get("selected", {})
        # Unconfirmed rows remain visible in export but never receive an
        # apparently final recipient assembled from an automatic suggestion.
        candidate = selected or next(iter(row.get("candidates", [])), {})
        evidence = candidate.get("evidence", [])
        org_url = next((e["url"] for e in evidence if e["type"] == "조직도"), "")
        api_url = next((e["url"] for e in evidence if e["type"] == "기관코드 API"), "")
        duty_url = candidate.get("url", "") if candidate.get("duty_verified") is not False else ""
        values = [row["location"], row.get("canonical", ""), candidate.get("bureau", ""), candidate.get("department", ""), candidate.get("team", ""), candidate.get("phone", ""), candidate.get("duty", ""), row.get("recipient", "") if row.get("confirmed") else "", "사용자 확인" if row.get("confirmed") else row["status"], duty_url, org_url, row.get("checked_at", ""), row.get("confirmed_at", ""), " / ".join(row.get("notes", [])), candidate.get("org_code", ""), candidate.get("org_full_name", ""), api_url]
        # Prevent spreadsheet formula execution when a filename, website or
        # user-entered recipient begins with a spreadsheet control character.
        writer.writerow(["'" + str(v) if str(v).startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else str(v) for v in values])
    return stream.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log user names, uploaded rows, request bodies or keys.

    def send_bytes(self, data, mime, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(data)

    def respond(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def valid_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def authenticated(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return "finder_session" in cookie and secrets.compare_digest(cookie["finder_session"].value, SESSION)
        except Exception:
            return False

    def do_GET(self):
        if not self.valid_host():
            self.respond({"error": "허용되지 않은 호스트입니다."}, 403)
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path in STATIC:
            filename, mime = STATIC[path]
            headers = {"Set-Cookie": f"finder_session={SESSION}; HttpOnly; SameSite=Strict; Path=/"} if path == "/" else None
            self.send_bytes((ROOT / "static" / filename).read_bytes(), mime + "; charset=utf-8", headers=headers)
            return
        if not self.authenticated():
            self.respond({"error": "화면을 새로 열어 주세요."}, 403)
            return
        with LOCK:
            if path == "/api/settings":
                self.respond({"naver_configured": all(credentials()), "public_configured": bool(public_key()), "version": "0.2"})
            elif path == "/api/saved":
                self.respond({"results": saved_results()})
            elif path == "/api/last":
                try:
                    self.respond(json.loads((DATA / "last-session.json").read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    self.respond({"results": []})
            elif path == "/api/export":
                params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                if params.get("mode", [""])[0] == "saved":
                    rows = saved_results()
                else:
                    job = JOBS.get(params.get("job_id", [""])[0])
                    if job:
                        rows = job["results"]
                    else:
                        try:
                            rows = json.loads((DATA / "last-session.json").read_text(encoding="utf-8"))["results"]
                        except (OSError, ValueError):
                            rows = []
                ids = set(params.get("ids", [""])[0].split(","))
                rows = [r for r in rows if r.get("id") in ids]
                self.send_bytes(csv_bytes(rows), "text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="departments.csv"'})
            elif path.startswith("/api/jobs/"):
                job_id = path.removeprefix("/api/jobs/")
                job = JOBS.get(job_id)
                self.respond(copy.deepcopy(job) if job else {"error": "조회 작업을 찾을 수 없습니다."}, 200 if job else 404)
            else:
                self.respond({"error": "경로를 찾을 수 없습니다."}, 404)

    def do_POST(self):
        if not self.valid_host() or not self.authenticated() or self.headers.get("Origin") not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            self.respond({"error": "로컬 화면에서 다시 요청해 주세요."}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 15_000_000:
                raise ValueError("요청이 너무 큽니다. 파일은 10MB 이하로 준비해 주세요.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("요청 형식을 확인해 주세요.")
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/settings":
                provider = payload.get("provider", "naver")
                if provider == "public":
                    key = str(payload.get("service_key", "")).strip()
                    if not key or len(key) > 1000 or re.search(r"\s|[\"']", key):
                        raise ValueError("일반 인증키만 입력해 주세요. 공백·줄바꿈·따옴표는 포함할 수 없습니다.")
                    save_settings({"PUBLIC_DATA_SERVICE_KEY": key})
                    self.respond({"ok": True, "public_configured": bool(public_key())})
                elif provider == "naver":
                    client_id = str(payload.get("client_id", "")).strip()
                    client_secret = str(payload.get("client_secret", "")).strip()
                    if not client_id or not client_secret or re.search(r"\s|[\"']", client_id + client_secret) or len(client_id + client_secret) > 1000:
                        raise ValueError("Client ID와 Client Secret을 모두 입력해 주세요.")
                    save_settings({"NAVER_CLIENT_ID": client_id, "NAVER_CLIENT_SECRET": client_secret})
                    self.respond({"ok": True, "naver_configured": True})
                else:
                    raise ValueError("API 종류를 확인해 주세요.")
            elif path == "/api/settings/test":
                from public_api import check_connection
                if not public_key():
                    raise ValueError("기관코드 API 인증키를 먼저 저장해 주세요.")
                self.respond({"ok": True, "message": check_connection(public_key())})
            elif path == "/api/upload":
                raw = base64.b64decode(payload.get("data", ""), validate=True)
                if len(raw) > 10_000_000:
                    raise ValueError("파일은 10MB 이하로 준비해 주세요.")
                self.respond(read_workbook(raw, str(payload.get("filename", ""))))
            elif path == "/api/search":
                if not isinstance(payload.get("locations"), list):
                    raise ValueError("지자체 목록을 확인해 주세요.")
                locations = list(dict.fromkeys(str(x).strip() for x in payload["locations"] if str(x).strip()))
                if not locations or len(locations) > 100 or any(len(x) > 100 for x in locations):
                    raise ValueError("한 번에 1~100개 지자체를 조회할 수 있습니다.")
                kind = payload.get("kind", "all")
                if kind not in ("all", "personal", "corporate", "special"):
                    raise ValueError("조회할 업무 종류를 확인해 주세요.")
                sources = payload.get("sources", {})
                if not isinstance(sources, dict):
                    raise ValueError("공식 URL 목록을 확인해 주세요.")
                with LOCK:
                    if any(j["state"] in ("queued", "running") for j in JOBS.values()):
                        raise ValueError("진행 중인 조회가 있습니다. 완료 후 다시 시작해 주세요.")
                    if len(JOBS) >= 20:
                        del JOBS[next(iter(JOBS))]
                    job_id = secrets.token_hex(12)
                    JOBS[job_id] = {"id": job_id, "state": "queued", "total": len(locations), "completed": 0, "current": "", "cancelled": False, "results": [], "started_at": stamp()}
                WORKERS.submit(run_job, job_id, locations, kind, sources)
                self.respond({"id": job_id}, 202)
            elif path == "/api/cancel":
                with LOCK:
                    job = JOBS.get(payload.get("id"))
                    if job:
                        job["cancelled"] = True
                self.respond({"ok": True})
            elif path == "/api/confirm":
                if payload.get("verified") is not True:
                    raise ValueError("근거 확인 체크를 선택해 주세요.")
                with LOCK:
                    job = JOBS.get(payload.get("job_id"))
                    rows = job["results"] if job else saved_results()
                    row = next((r for r in rows if r.get("id") == payload.get("row_id")), None)
                    if row is None:
                        try:
                            last = json.loads((DATA / "last-session.json").read_text(encoding="utf-8"))
                            row = next((r for r in last["results"] if r["id"] == payload.get("row_id")), None)
                        except (OSError, ValueError):
                            pass
                    if not row:
                        raise ValueError("조회 결과를 찾을 수 없습니다. 다시 조회해 주세요.")
                    index = payload.get("candidate_index", -1)
                    candidate = copy.deepcopy(row["candidates"][index]) if isinstance(index, int) and 0 <= index < len(row["candidates"]) else copy.deepcopy(row.get("selected", {"evidence": []}))
                    previous = copy.deepcopy(candidate)
                    for field in ("bureau", "department", "team", "phone", "duty", "url"):
                        candidate[field] = str(payload.get(field, candidate.get(field, ""))).strip()[:2000]
                    canonical = str(payload.get("canonical", row["canonical"])).strip()[:100]
                    if not candidate["department"] or not canonical or not candidate["url"].startswith(("https://", "http://")):
                        raise ValueError("기관명, 부서명, 확인 근거 URL을 입력해 주세요.")
                    if any(candidate.get(k) != previous.get(k) for k in ("bureau", "department", "url")):
                        candidate["evidence"] = []
                        candidate.pop("org_code", None)
                        candidate.pop("org_full_name", None)
                    candidate["evidence"].append({"type": "사용자 확인", "url": candidate["url"], "text": f"{canonical} / {candidate['bureau']} / {candidate['department']}（직접 확인）"})
                    row.update({"canonical": canonical, "confirmed": True, "status": "사용자 확인", "selected": candidate, "confirmed_at": stamp(), "recipient": f"{canonical}({candidate['department']})"})
                    saved = [r for r in saved_results() if location_key(r["location"]) != location_key(row["location"])]
                    saved.append(copy.deepcopy(row))
                    save_json(DATA / "confirmed.json", saved)
                    if job and job["state"] in ("done", "cancelled"):
                        save_json(DATA / "last-session.json", job)
                    self.respond(copy.deepcopy(row))
            elif path == "/api/export":
                rows = payload.get("results", [])
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ValueError("내보낼 결과를 확인해 주세요.")
                self.send_bytes(csv_bytes(rows), "text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="departments.csv"'})
            else:
                self.respond({"error": "경로를 찾을 수 없습니다."}, 404)
        except ValueError as exc:
            self.respond({"error": str(exc)}, 400)
        except Exception:
            self.respond({"error": "처리하지 못했습니다. 파일 또는 입력값을 확인해 주세요."}, 500)


def main():
    global PORT
    parser = argparse.ArgumentParser(description="지방소득세 담당 부서 찾기")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    PORT = args.port
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Department Finder: {url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        WORKERS.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
