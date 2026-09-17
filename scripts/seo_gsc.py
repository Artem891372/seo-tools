# -*- coding: utf-8 -*-
"""Google Search Console API: OAuth (Desktop app) + выгрузка SEO-данных в var/seo/gsc.

Разовый CLI-инструмент по образцу scripts/yandex_webmaster.py: без инфраструктуры,
только стандартная библиотека 3.11+. Поддерживает OAuth с локальным коллбэком
(Desktop app), refresh-токен хранится в var/seo/gsc_token.json (chmod 600).

Подготовка ключей (один раз):
  1. console.cloud.google.com → APIs & Services → Library → включить
     «Google Search Console API».
  2. OAuth consent screen: External; добавьте свой аккаунт в Test users.
  3. Credentials → Create credentials → OAuth client ID → тип «Desktop app».
  4. GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET → .env.prod.

Запуск (из корня репо):
  python3 scripts/seo_gsc.py auth            # откроет браузер, поймает код на localhost
  python3 scripts/seo_gsc.py sites           # список ресурсов, доступных токену
  python3 scripts/seo_gsc.py fetch [--days 90] [--site <подстрока>]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://searchconsole.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"

DEFAULT_ENV_FILE = REPO / ".env.prod"
DEFAULT_TOKEN_FILE = REPO / "var" / "seo" / "gsc_token.json"
DEFAULT_OUT_DIR = REPO / "var" / "seo" / "gsc"
DEFAULT_REDIRECT_PORT = 8765
ROW_LIMIT = 1000


class SeoError(RuntimeError):
    """Ошибка с человекочитаемой подсказкой (печатается без трейсбека)."""


class ApiError(SeoError):
    """Ошибка Search Console API: HTTP-код + тело ответа."""

    def __init__(self, status: int, message: str, path: str):
        self.status, self.message, self.path = status, message, path
        super().__init__(f"HTTP {status} на {path}: {message}")


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(env_file: Path) -> dict[str, str]:
    merged = dict(load_env_file(env_file))
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "WEBSITE_DOMAIN"):
        if os.environ.get(key):
            merged[key] = os.environ[key]
    return merged


def _request_json(url: str, *, method: str = "GET", form: dict | None = None,
                  token: str | None = None, retries: int = 3, timeout: int = 60) -> dict:
    body = urllib.parse.urlencode(form).encode("ascii") if form else None
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if form else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            payload = _safe_json(exc.read())
            if exc.code in (429, 500, 502, 503) and attempt < retries:
                delay = int(exc.headers.get("Retry-After") or 2 ** attempt)
                print(f"[api] HTTP {exc.code}, повтор через {delay} с …")
                time.sleep(delay)
                continue
            message = (payload.get("error", {}).get("message")
                       if isinstance(payload.get("error"), dict)
                       else payload.get("error_description") or payload.get("error") or "")
            raise ApiError(exc.code, str(message), url) from exc
    raise ApiError(0, "исчерпаны повторы", url)


def _safe_json(blob: bytes) -> dict:
    try:
        data = json.loads(blob.decode("utf-8"))
        return data if isinstance(data, dict) else {"_": data}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def exchange_code(config: dict[str, str], code: str, redirect_uri: str) -> dict:
    return _request_json(TOKEN_URL, method="POST", form={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config["GOOGLE_CLIENT_ID"],
        "client_secret": config["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri,
    })


def refresh_access_token(config: dict[str, str], refresh_token: str) -> dict:
    return _request_json(TOKEN_URL, method="POST", form={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config["GOOGLE_CLIENT_ID"],
        "client_secret": config["GOOGLE_CLIENT_SECRET"],
    })


def write_token_file(path: Path, payload: dict, previous: dict | None = None) -> dict:
    payload = dict(payload)
    if not payload.get("refresh_token") and previous:
        payload["refresh_token"] = previous.get("refresh_token")
    payload["expires_at"] = int(time.time()) + int(payload.get("expires_in", 0))
    payload["obtained_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return payload


def read_token_file(path: Path) -> dict:
    if not path.is_file():
        raise SeoError(f"Нет токена {path}. Сначала выполните: "
                       f"python3 scripts/seo_gsc.py auth")
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_access_token(config: dict[str, str], token_file: Path) -> str:
    stored = read_token_file(token_file)
    if stored.get("expires_at", 0) - 120 > time.time():
        return stored["access_token"]
    if not stored.get("refresh_token"):
        raise SeoError("Токен истёк, refresh_token отсутствует — повторите auth.")
    fresh = refresh_access_token(config, stored["refresh_token"])
    stored = write_token_file(token_file, fresh, previous=stored)
    print(f"[auth] access_token продлён до "
          f"{datetime.fromtimestamp(stored['expires_at']).isoformat(timespec='minutes')}")
    return stored["access_token"]


def api_get(token: str, path: str) -> dict:
    return _request_json(f"{API_BASE}{path}", token=token)


def api_post(token: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=json.dumps(body).encode("utf-8"),
        method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        payload = _safe_json(exc.read())
        error = payload.get("error") or {}
        message = error.get("message", "") if isinstance(error, dict) else str(error)
        raise ApiError(exc.code, message, path) from exc


def list_sites(token: str) -> list[dict]:
    return list(api_get(token, "/webmasters/v3/sites").get("siteEntry", []))


def pick_site(sites: list[dict], needle: str | None, website_domain: str | None) -> dict:
    """Выбрать ресурс GSC: явная подстрока → WEBSITE_DOMAIN → единственный."""
    def matches(site: dict, token: str) -> bool:
        return token.lower() in str(site.get("siteUrl", "")).lower()

    if needle:
        found = [s for s in sites if matches(s, needle)]
        if len(found) != 1:
            raise SeoError(f"--site {needle!r}: совпадений {len(found)}, нужно ровно одно. "
                           f"Доступные: {', '.join(s['siteUrl'] for s in sites)}")
        return found[0]

    if website_domain:
        parts = urllib.parse.urlsplit(website_domain)
        netloc = parts.netloc or website_domain
        hostname = urllib.parse.urlsplit(f"//{netloc}").hostname or netloc
        candidates = [hostname]
        try:
            candidates.append(hostname.encode("idna").decode("ascii"))
        except UnicodeError:
            pass
        found = [s for s in sites if any(matches(s, name) for name in candidates)]
        if len(found) == 1:
            return found[0]

    if len(sites) == 1:
        return sites[0]
    raise SeoError("Не понимаю, какой ресурс брать: подходящих несколько. "
                   "Укажите --site. Доступные: " + ", ".join(s["siteUrl"] for s in sites))


def search_analytics(token: str, site_url: str, body: dict) -> list[dict]:
    path = f"/webmasters/v3/sites/{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query"
    return list(api_post(token, path, body).get("rows", []))


def inspect_url(token: str, site_url: str, inspection_url: str) -> dict:
    payload = api_post(token, "/v1/urlInspection/index:inspect", {
        "inspectionUrl": inspection_url,
        "siteUrl": site_url,
        "languageCode": "ru",
    })
    return payload.get("inspectionResult") or {}


def site_dir(base: Path, site_url: str) -> Path:
    slug = (site_url.replace("https://", "").replace("http://", "")
            .replace("sc-domain:", "domain_").strip("/").replace("/", "_") or "site")
    return base / slug


def print_inspection(url: str, result: dict) -> None:
    if result.get("error"):
        print(f"[fail] {url}: {result['error']}")
        return
    status = result.get("indexStatusResult") or {}
    print(f"[ok]   {url}")
    for key in ("verdict", "coverageState", "robotsTxtState", "indexingState",
                "pageFetchState", "lastCrawlTime", "googleCanonical", "userCanonical"):
        if status.get(key):
            print(f"       {key}: {status[key]}")
    if (status.get("userCanonical") and status.get("googleCanonical")
            and status["userCanonical"] != status["googleCanonical"]):
        print("       ! canonical не совпадает с выбранным Google")


def query_rows(token: str, site_url: str, dimension: str, days: int,
               limit: int = ROW_LIMIT) -> list[dict]:
    """Страницы по ROW_LIMIT строк; keys[0] разворачиваем в поле dimension."""
    end = date.today()
    start = end - timedelta(days=days)
    rows: list[dict] = []
    while len(rows) < limit:
        page = search_analytics(token, site_url, {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": [dimension],
            "rowLimit": min(ROW_LIMIT, limit - len(rows)),
            "startRow": len(rows),
        })
        rows.extend(page)
        if len(page) < ROW_LIMIT:
            break
    out = []
    for row in rows:
        keys = row.get("keys") or [""]
        out.append({
            dimension: keys[0] if keys else "",
            "clicks": row.get("clicks"),
            "impressions": row.get("impressions"),
            "ctr": row.get("ctr"),
            "position": row.get("position"),
        })
    return out


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_analytics(out: Path, name: str, dimension: str, rows: list[dict]) -> None:
    fields = [dimension, "clicks", "impressions", "ctr", "position"]
    write_json(out / f"{name}.json", rows)
    write_csv(out / f"{name}.csv", rows, fields)


def build_summary(site_url: str, data: dict, errors: dict[str, str], days: int) -> str:
    lines = [
        "# Google Search Console — сводка выгрузки", "",
        f"- Ресурс: {site_url}",
        f"- Период: последние {days} дн.",
        f"- Выгружено: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
    ]
    dates = data.get("dates") or []
    total_clicks = sum((r.get("clicks") or 0) for r in dates)
    total_impressions = sum((r.get("impressions") or 0) for r in dates)
    lines += ["## Итоги за период", "",
              f"- Клики: {total_clicks:g}",
              f"- Показы: {total_impressions:g}", ""]
    if total_impressions == 0:
        lines += ["Показов нет. Проверьте: тип ресурса (URL-prefix http/https или "
                  "sc-domain), свежесть индексации и что сайт не закрыт для Googlebot.", ""]

    queries = data.get("queries") or []
    if queries:
        lines += ["## Топ-20 запросов", "",
                  "| запрос | клики | показы | CTR | позиция |", "|---|---:|---:|---:|---:|"]
        for row in queries[:20]:
            lines.append("| {q} | {c} | {i} | {ctr:.1%} | {p:.1f} |".format(
                q=str(row.get("query", "")).replace("|", "\\|"),
                c=row.get("clicks") or 0, i=row.get("impressions") or 0,
                ctr=row.get("ctr") or 0, p=row.get("position") or 0))
        lines.append("")

    pages = data.get("pages") or []
    if pages:
        lines += ["## Топ-20 страниц", "",
                  "| страница | клики | показы | позиция |", "|---|---:|---:|---:|"]
        for row in pages[:20]:
            lines.append("| {u} | {c} | {i} | {p:.1f} |".format(
                u=str(row.get("page", "")).replace("|", "\\|"),
                c=row.get("clicks") or 0, i=row.get("impressions") or 0,
                p=row.get("position") or 0))
        lines.append("")

    sitemaps = (data.get("sitemaps") or {}).get("sitemap") or []
    lines += ["## Sitemap", ""]
    if sitemaps:
        for item in sitemaps:
            contents = item.get("contents") or [{}]
            counts = ", ".join(f"{c.get('type')}: {c.get('submitted')}/{c.get('indexed')}"
                               for c in contents)
            lines.append(f"- {item.get('path')}: тип {item.get('type')}, "
                         f"ошибки {item.get('errors')}, предупреждения {item.get('warnings')}, "
                         f"URL (отправлено/проиндексировано) {counts}")
    else:
        lines.append("Sitemap в ресурсе не зарегистрированы.")
    lines.append("")

    lines += ["## Ошибки выгрузки", ""]
    lines += [f"- {name}: {message}" for name, message in errors.items()] or ["Нет."]
    lines.append("")
    return "\n".join(lines)


def save_host_data(out: Path, site_url: str, data: dict, errors: dict[str, str],
                   days: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name in ("queries", "pages", "dates", "countries", "devices"):
        if name in data:
            save_analytics(out, name, {"dates": "date", "queries": "query",
                                       "pages": "page", "countries": "country",
                                       "devices": "device"}[name], data[name])
    if "sitemaps" in data:
        write_json(out / "sitemaps.json", data["sitemaps"])
    write_json(out / "run_meta.json", {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site_url": site_url, "days": days, "errors": errors,
    })
    (out / "SUMMARY.md").write_text(build_summary(site_url, data, errors, days),
                                    encoding="utf-8")


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        _CallbackHandler.code = (params.get("code") or [None])[0]
        _CallbackHandler.error = (params.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>Готово, можно закрыть окно.</h2>".encode("utf-8"))

    def log_message(self, *args):
        pass


def build_auth_url(config: dict[str, str], redirect_uri: str) -> str:
    return (f"{AUTH_URL}?response_type=code"
            f"&client_id={urllib.parse.quote(config['GOOGLE_CLIENT_ID'])}"
            f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
            f"&scope={urllib.parse.quote(SCOPE, safe='')}"
            f"&access_type=offline&prompt=consent")


def run_auth_flow(config: dict[str, str], port: int, timeout: int = 300,
                  redirect_host: str = "127.0.0.1") -> str:
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    actual_port = server.server_address[1]
    redirect_uri = f"http://{redirect_host}:{actual_port}/"
    print("Откройте ссылку под Google-аккаунтом, у которого есть доступ в Search Console:\n")
    print(build_auth_url(config, redirect_uri) + "\n")
    server.timeout = timeout
    try:
        server.handle_request()
    finally:
        server.server_close()
    if _CallbackHandler.error:
        raise SeoError(f"Google вернул ошибку авторизации: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise SeoError("Код не получен (таймаут). Повторите auth.")
    return redirect_uri


def run_auth_flow_manual(config: dict[str, str], port: int,
                         redirect_url: str | None = None,
                         redirect_host: str = "127.0.0.1") -> tuple[str, str]:
    """Ручной режим для SSH: браузер откроется не на сервере — вставляем URL целиком."""
    redirect_uri = f"http://{redirect_host}:{port}/"
    print("Откройте ссылку в браузере:\n")
    print(build_auth_url(config, redirect_uri) + "\n")
    if redirect_url is None:
        try:
            redirect_url = input("Вставьте полный URL из адресной строки после редиректа: ")
        except EOFError:
            raise SeoError("URL не введён (неинтерактивный запуск).")
    pasted = redirect_url.strip()
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
    if params.get("error"):
        raise SeoError(f"Google вернул ошибку авторизации: {params['error'][0]}")
    code = (params.get("code") or [None])[0]
    if not code:
        raise SeoError("В URL нет параметра code — скопируйте адрес целиком после редиректа.")
    return code, redirect_uri


def cmd_auth(args, config: dict[str, str]) -> int:
    if not config.get("GOOGLE_CLIENT_ID") or not config.get("GOOGLE_CLIENT_SECRET"):
        raise SeoError("В .env.prod нет GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.")
    if args.manual:
        code, redirect_uri = run_auth_flow_manual(config, args.port, args.redirect_url,
                                                  args.redirect_host)
    else:
        redirect_uri = run_auth_flow(config, args.port, args.timeout, args.redirect_host)
        code = _CallbackHandler.code or ""
    payload = exchange_code(config, code, redirect_uri)
    stored = write_token_file(args.token_file, payload)
    print(f"[auth] Токен сохранён: {args.token_file} (chmod 600)")
    print(f"[auth] scope: {stored.get('scope')}")
    print(f"[auth] действует до: "
          f"{datetime.fromtimestamp(stored['expires_at']).isoformat(timespec='minutes')}")
    return 0


def cmd_sites(args, config: dict[str, str]) -> int:
    token = ensure_access_token(config, args.token_file)
    sites = list_sites(token)
    print(f"ресурсов: {len(sites)}\n")
    for site in sites:
        print(f"- {site.get('siteUrl')}  ({site.get('permissionLevel')})")
    return 0


def cmd_fetch(args, config: dict[str, str]) -> int:
    token = ensure_access_token(config, args.token_file)
    sites = list_sites(token)
    if not sites:
        raise SeoError("Токену не доступен ни один ресурс: подтвердите сайт в Search Console "
                       "под этим аккаунтом.")
    site = pick_site(sites, args.site, config.get("WEBSITE_DOMAIN"))
    site_url = str(site["siteUrl"])
    out = site_dir(args.out_dir, site_url)
    print(f"[site] {site_url} → {out}")
    write_json(out / "sites_all.json", {"sites": sites})

    data: dict = {}
    errors: dict[str, str] = {}

    def attempt(name: str, fn):
        try:
            data[name] = fn()
            print(f"[ok]   {name}")
        except (ApiError, SeoError) as exc:
            errors[name] = str(exc)
            print(f"[fail] {name}: {exc}", file=sys.stderr)

    qpath = urllib.parse.quote(site_url, safe="")
    attempt("queries", lambda: query_rows(token, site_url, "query", args.days))
    attempt("pages", lambda: query_rows(token, site_url, "page", args.days))
    attempt("dates", lambda: query_rows(token, site_url, "date", args.days))
    attempt("countries", lambda: query_rows(token, site_url, "country", args.days))
    attempt("devices", lambda: query_rows(token, site_url, "device", args.days))
    attempt("sitemaps", lambda: api_get(token, f"/webmasters/v3/sites/{qpath}/sitemaps"))

    save_host_data(out, site_url, data, errors, args.days)
    print(f"\nГотово: {out / 'SUMMARY.md'}")
    if errors:
        print(f"С ошибками по {len(errors)} ручкам (см. run_meta.json): "
              f"{', '.join(errors)}", file=sys.stderr)
    return 0


def cmd_inspect(args, config: dict[str, str]) -> int:
    token = ensure_access_token(config, args.token_file)
    sites = list_sites(token)
    if not sites:
        raise SeoError("Токену не доступен ни один ресурс Search Console.")
    site = pick_site(sites, args.site, config.get("WEBSITE_DOMAIN"))
    site_url = str(site["siteUrl"])
    out = site_dir(args.out_dir, site_url)
    results: dict[str, dict] = {}
    for url in args.urls:
        try:
            results[url] = inspect_url(token, site_url, url)
        except (ApiError, SeoError) as exc:
            results[url] = {"error": str(exc)}
        print_inspection(url, results[url])
        time.sleep(1)
    write_json(out / "inspection.json", results)
    print(f"\nГотово: {out / 'inspection.json'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seo_gsc.py",
        description="Google Search Console API: OAuth и выгрузка SEO-данных (var/seo/gsc).",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="OAuth с локальным коллбэком (Desktop app)")
    auth.add_argument("--port", type=int, default=DEFAULT_REDIRECT_PORT,
                      help="порт локального коллбэка (по умолчанию 8765)")
    auth.add_argument("--manual", action="store_true",
                      help="ручной режим: вставить URL после редиректа (браузер не на сервере)")
    auth.add_argument("--redirect-url", default=None,
                      help="URL после редиректа (для неинтерактивного ручного режима)")
    auth.add_argument("--timeout", type=int, default=300,
                      help="сколько секунд ждать код на коллбэке (по умолчанию 300)")
    auth.add_argument("--redirect-host", default="127.0.0.1",
                      help="хост в redirect_uri: 127.0.0.1 (по умолчанию) или localhost")
    auth.set_defaults(func=cmd_auth)

    sites = sub.add_parser("sites", help="список ресурсов Search Console")
    sites.set_defaults(func=cmd_sites)

    fetch = sub.add_parser("fetch", help="выгрузить данные по ресурсу")
    fetch.add_argument("--site", default=None,
                       help="подстрока siteUrl (иначе WEBSITE_DOMAIN или единственный)")
    fetch.add_argument("--days", type=int, default=90,
                       help="глубина истории, дней (по умолчанию 90)")
    fetch.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    fetch.set_defaults(func=cmd_fetch)

    inspect = sub.add_parser("inspect", help="статус URL в индексе Google (URL Inspection API)")
    inspect.add_argument("urls", nargs="+", help="URL(-ы) для проверки")
    inspect.add_argument("--site", default=None,
                         help="подстрока siteUrl (иначе WEBSITE_DOMAIN или единственный)")
    inspect.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    inspect.set_defaults(func=cmd_inspect)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.env_file)
        return args.func(args, config)
    except SeoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
