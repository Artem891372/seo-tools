# -*- coding: utf-8 -*-
"""Яндекс.Вебмастер API v4: OAuth + выгрузка SEO-данных в var/seo/yandex.

Разовый CLI-инструмент (без инфраструктуры): GSC и Bing Webmaster — отдельными
скриптами рядом, когда дойдут руки. Зависимостей нет — только стандартная
библиотека, скрипт запускается любым python3 3.11+.

Подкоманды:
  auth   — получить OAuth-токен. Redirect URI приложения в oauth.yandex.ru
           должен быть https://oauth.yandex.ru/verification_code (Яндекс покажет
           код на экране — скрипт его спросит). Refresh-токен сохраняется и
           дальше продлевается автоматически.
  hosts  — список сайтов, доступных токену (id, URL, верификация).
  fetch  — выгрузить по сайту: индексация, поисковые запросы (популярные за
           последнюю неделю + история), диагностика, sitemap, важные URL.
           Пишет сырые JSON + CSV + SUMMARY.md в var/seo/yandex/<хост>/.
  recrawl — поставить URL на переобход (лимит Вебмастера 150/день). Без --url
           берёт все ссылки из sitemap сайта; задачи пишет в recrawl_tasks.json.

Ключи приложения берутся из окружения или из .env.prod (YANDEX_CLIENT_ID /
YANDEX_CLIENT_SECRET); WEBSITE_DOMAIN из .env.prod помогает выбрать сайт.

Запуск (из корня репо):
  python3 scripts/yandex_webmaster.py auth
  python3 scripts/yandex_webmaster.py fetch [--days 90] [--host <подстрока>]
  python3 scripts/yandex_webmaster.py recrawl [--limit N] [--url URL ...] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

OAUTH_AUTHORIZE = "https://oauth.yandex.ru/authorize"
OAUTH_TOKEN = "https://oauth.yandex.ru/token"
API_BASE = "https://api.webmaster.yandex.net/v4"
SCOPE = "webmaster:hostinfo webmaster:verify"

DEFAULT_ENV_FILE = REPO / ".env.prod"
DEFAULT_TOKEN_FILE = REPO / "var" / "seo" / "yandex_token.json"
DEFAULT_OUT_DIR = REPO / "var" / "seo" / "yandex"

POPULAR_ORDERINGS = {"shows": "TOTAL_SHOWS", "clicks": "TOTAL_CLICKS"}
POPULAR_PAGE = 500


class SeoError(RuntimeError):
    """Ошибка с человекочитаемой подсказкой (печатается без трейсбека)."""


class ApiError(SeoError):
    """Ошибка API Вебмастера: HTTP-код + error_code/error_message из тела."""

    def __init__(self, status: int, code: str, message: str, path: str):
        self.status, self.code, self.message, self.path = status, code, message, path
        super().__init__(f"HTTP {status} {code} на {path}: {message}")


def load_env_file(path: Path) -> dict[str, str]:
    """Простой KEY=VALUE парсер .env (без интерполяции и экспорта в os.environ)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def load_config(env_file: Path) -> dict[str, str]:
    """Приоритет: переменные окружения → .env.prod; .env дополняет недостающее."""
    file_values = load_env_file(env_file)
    merged = dict(file_values)
    for key in ("YANDEX_CLIENT_ID", "YANDEX_CLIENT_SECRET", "WEBSITE_DOMAIN"):
        if os.environ.get(key):
            merged[key] = os.environ[key]
    return merged


def _post_form(url: str, form: list[tuple[str, str]], timeout: int = 30) -> dict:
    body = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = _safe_json(exc.read())
        error = payload.get("error", f"HTTP {exc.code}")
        description = payload.get("error_description", "")
        hint = ""
        if error == "invalid_client":
            hint = ("\nПодсказка: проверьте YANDEX_CLIENT_SECRET в .env.prod — это "
                    "секрет приложения с oauth.yandex.ru, он НЕ равен client_id.")
        raise SeoError(f"OAuth-ошибка: {error}. {description}{hint}") from exc


def _safe_json(blob: bytes) -> dict:
    try:
        data = json.loads(blob.decode("utf-8"))
        return data if isinstance(data, dict) else {"_": data}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def exchange_code(config: dict[str, str], code: str) -> dict:
    return _post_form(OAUTH_TOKEN, [
        ("grant_type", "authorization_code"),
        ("code", code),
        ("client_id", config["YANDEX_CLIENT_ID"]),
        ("client_secret", config["YANDEX_CLIENT_SECRET"]),
    ])


def refresh_access_token(config: dict[str, str], refresh_token: str) -> dict:
    return _post_form(OAUTH_TOKEN, [
        ("grant_type", "refresh_token"),
        ("refresh_token", refresh_token),
        ("client_id", config["YANDEX_CLIENT_ID"]),
        ("client_secret", config["YANDEX_CLIENT_SECRET"]),
    ])


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
                       f"python3 scripts/yandex_webmaster.py auth")
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_access_token(config: dict[str, str], token_file: Path) -> str:
    """Вернуть живой access_token: при истечении — обновить по refresh-токену."""
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


def api_get(access_token: str, path: str, params: list[tuple[str, str]] | None = None,
            *, retries: int = 3, timeout: int = 60) -> dict:
    """GET к API Вебмастера; повторяет 429/5xx, ошибки отдаёт как ApiError."""
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"Authorization": f"OAuth {access_token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = _safe_json(exc.read())
            if exc.code in (429, 500, 502, 503) and attempt < retries:
                delay = int(exc.headers.get("Retry-After") or 2 ** attempt)
                print(f"[api] HTTP {exc.code}, повтор через {delay} с …")
                time.sleep(delay)
                continue
            raise ApiError(
                exc.code,
                payload.get("error_code", f"HTTP_{exc.code}"),
                payload.get("error_message", ""),
                path,
            ) from exc
    raise ApiError(0, "RETRIES_EXHAUSTED", "исчерпаны повторы", path)


def api_post(access_token: str, path: str, payload: dict,
             *, retries: int = 3, timeout: int = 60) -> dict:
    """POST JSON к API Вебмастера; повторяет 429/5xx, ошибки отдаёт как ApiError."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            f"{API_BASE}{path}", data=body, method="POST",
            headers={"Authorization": f"OAuth {access_token}",
                     "Content-Type": "application/json;charset=UTF-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload_err = _safe_json(exc.read())
            if exc.code in (429, 500, 502, 503) and attempt < retries:
                delay = int(exc.headers.get("Retry-After") or 2 ** attempt)
                print(f"[api] HTTP {exc.code}, повтор через {delay} с …")
                time.sleep(delay)
                continue
            raise ApiError(
                exc.code,
                payload_err.get("error_code", f"HTTP_{exc.code}"),
                payload_err.get("error_message", ""),
                path,
            ) from exc
    raise ApiError(0, "RETRIES_EXHAUSTED", "исчерпаны повторы", path)


def fetch_sitemap_urls(sitemap_url: str, timeout: int = 30) -> list[str]:
    """Список <loc> из sitemap.xml (простой парсер без XML-зависимостей)."""
    req = urllib.request.Request(sitemap_url, headers={"User-Agent": "seo-tools/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        xml = resp.read().decode("utf-8", "replace")
    seen: set[str] = set()
    urls: list[str] = []
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
        if loc not in seen:
            seen.add(loc)
            urls.append(loc)
    return urls


def get_user_id(access_token: str) -> int:
    try:
        return int(api_get(access_token, "/user")["user_id"])
    except ApiError as exc:
        if exc.code == "INVALID_OAUTH_TOKEN":
            raise SeoError(
                "API не принял токен. Проверьте, что приложению в oauth.yandex.ru "
                "выдан доступ «Яндекс.Вебмастер» (scope webmaster:hostinfo), "
                "и повторите auth."
            ) from exc
        raise


def list_hosts(access_token: str, user_id: int) -> list[dict]:
    return list(api_get(access_token, f"/user/{user_id}/hosts").get("hosts", []))


def pick_host(hosts: list[dict], needle: str | None, website_domain: str | None) -> dict:
    """Выбрать сайт: явная подстрока → домен из WEBSITE_DOMAIN → единственный хост."""
    def matches(host: dict, token: str) -> bool:
        haystack = " ".join(str(host.get(k, "")) for k in
                            ("host_id", "ascii_host_url", "unicode_host_url")).lower()
        return token.lower() in haystack

    if needle:
        found = [h for h in hosts if matches(h, needle)]
        if len(found) != 1:
            raise SeoError(f"--host {needle!r}: совпадений {len(found)}, нужно ровно одно. "
                           f"Доступные: {', '.join(h['host_id'] for h in hosts)}")
        return found[0]

    if website_domain:
        parts = urllib.parse.urlsplit(website_domain)
        scheme = parts.scheme.lower()
        netloc = parts.netloc or website_domain
        hostname = urllib.parse.urlsplit(f"//{netloc}").hostname or netloc
        names = [hostname]
        try:
            names.append(hostname.encode("idna").decode("ascii"))
        except UnicodeError:
            pass
        found = [
            h for h in hosts
            if any(matches(h, name) for name in names)
            and (not scheme or str(h.get("host_id", "")).startswith(f"{scheme}:"))
        ]
        if len(found) == 1:
            return found[0]

    if len(hosts) == 1:
        return hosts[0]
    raise SeoError("Не понимаю, какой сайт брать: подходящих хостов несколько. "
                   "Укажите --host. Доступные: " + ", ".join(h["host_id"] for h in hosts))


def host_slug(host_id: str) -> str:
    return re.sub(r"[^0-9A-Za-zа-яА-ЯёЁ._-]+", "_", host_id).strip("_") or "host"


def host_path(user_id: int, host_id: str) -> str:
    """Путь до хоста; host_id квотируется (IDN/спецсимволы), двоеточия — как в доках."""
    return f"/user/{user_id}/hosts/{urllib.parse.quote(host_id, safe=':')}"


def _iso_dates(days: int) -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def fetch_popular(access_token: str, user_id: int, host_id: str, order_by: str,
                  cap: int) -> dict:
    """Постранично (limit=500) собрать популярные запросы; API отдаёт максимум ТОП-3000."""
    queries: list[dict] = []
    offset, meta = 0, {}
    while offset < cap:
        page = api_get(access_token,
                       f"{host_path(user_id, host_id)}/search-queries/popular", [
            ("order_by", order_by),
            ("query_indicator", order_by),
            ("limit", str(min(POPULAR_PAGE, cap - offset))),
            ("offset", str(offset)),
        ])
        meta = {k: page.get(k) for k in ("date_from", "date_to", "count") if k in page}
        chunk = list(page.get("queries", []))
        queries.extend(chunk)
        if len(chunk) < POPULAR_PAGE:
            break
        offset += len(chunk)
    return {"order_by": order_by, "queries": queries, **meta}


def collect_host_data(access_token: str, user_id: int, host_id: str,
                      days: int) -> tuple[dict, dict[str, str]]:
    """Собрать все срезы по хосту; ошибки отдельных ручек не роняют выгрузку."""
    date_from, date_to = _iso_dates(days)
    base = host_path(user_id, host_id)
    data: dict = {}
    errors: dict[str, str] = {}

    def attempt(name: str, fn):
        try:
            data[name] = fn()
            print(f"[ok]   {name}")
        except (ApiError, SeoError) as exc:
            errors[name] = str(exc)
            print(f"[fail] {name}: {exc}", file=sys.stderr)

    attempt("host_info", lambda: api_get(access_token, base))
    attempt("indexing_history", lambda: api_get(access_token, f"{base}/indexing/history", [
        ("date_from", date_from), ("date_to", date_to)]))
    attempt("search_queries_history", lambda: api_get(
        access_token, f"{base}/search-queries/all/history", [
        ("query_indicator", "TOTAL_SHOWS"),
        ("query_indicator", "TOTAL_CLICKS"),
        ("date_from", date_from), ("date_to", date_to)]))
    attempt("popular_by_shows", lambda: fetch_popular(
        access_token, user_id, host_id, POPULAR_ORDERINGS["shows"], 3000))
    attempt("popular_by_clicks", lambda: fetch_popular(
        access_token, user_id, host_id, POPULAR_ORDERINGS["clicks"], 3000))
    attempt("diagnostics", lambda: api_get(access_token, f"{base}/diagnostics"))
    attempt("important_urls", lambda: api_get(access_token, f"{base}/important-urls"))

    def sitemaps_with_details() -> dict:
        listing = api_get(access_token, f"{base}/sitemaps")
        details = {}
        for item in listing.get("sitemaps", []):
            sitemap_id = item.get("sitemap_id")
            if sitemap_id:
                try:
                    details[sitemap_id] = api_get(access_token, f"{base}/sitemaps/{sitemap_id}")
                except ApiError as exc:
                    details[sitemap_id] = {"error": str(exc)}
        return {**listing, "details": details}

    attempt("sitemaps", sitemaps_with_details)
    attempt("user_added_sitemaps", lambda: api_get(
        access_token, f"{base}/user-added-sitemaps"))
    return data, errors


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_indexing_history(out: Path, payload: dict) -> None:
    indicators = payload.get("indicators", {})
    by_date: dict[str, dict] = {}
    for name, points in indicators.items():
        for point in points:
            day = str(point.get("date", ""))[:10]
            by_date.setdefault(day, {})[name] = point.get("value")
    names = sorted(indicators)
    rows = [{"date": day, **values} for day, values in sorted(by_date.items())]
    write_json(out / "indexing_history.json", payload)
    write_csv(out / "indexing_history.csv", rows, ["date", *names])


def save_search_queries_history(out: Path, payload: dict) -> None:
    indicators = payload.get("indicators", {})
    by_date: dict[str, dict] = {}
    for name, points in indicators.items():
        for point in points:
            day = str(point.get("date", ""))[:10]
            by_date.setdefault(day, {})[name] = point.get("value")
    names = sorted(indicators)
    rows = [{"date": day, **values} for day, values in sorted(by_date.items())]
    write_json(out / "search_queries_history.json", payload)
    write_csv(out / "search_queries_history.csv", rows, ["date", *names])


def save_popular(out: Path, key: str, payload: dict) -> None:
    indicator_names: list[str] = []
    rows = []
    for query in payload.get("queries", []):
        row = {"query_text": query.get("query_text"), "query_id": query.get("query_id")}
        for name, value in (query.get("indicators") or {}).items():
            row[name] = value
            if name not in indicator_names:
                indicator_names.append(name)
        rows.append(row)
    fields = ["query_text", "query_id", *indicator_names]
    write_json(out / f"popular_{key}.json", payload)
    write_csv(out / f"popular_{key}.csv", rows, fields)


def save_important_urls(out: Path, payload: dict) -> None:
    rows = []
    for item in payload.get("urls", []):
        indexing = item.get("indexing_status") or {}
        search = item.get("search_status") or {}
        rows.append({
            "url": item.get("url"),
            "update_date": item.get("update_date"),
            "indexing_status": indexing.get("status"),
            "http_code": indexing.get("http_code"),
            "searchable": search.get("searchable"),
            "excluded_url_status": search.get("excluded_url_status"),
            "title": search.get("title"),
            "description": search.get("description"),
        })
    write_json(out / "important_urls.json", payload)
    write_csv(out / "important_urls.csv", rows, [
        "url", "update_date", "indexing_status", "http_code", "searchable",
        "excluded_url_status", "title", "description",
    ])


def save_sitemaps(out: Path, payload: dict) -> None:
    listing = payload.get("sitemaps", [])
    rows = [{
        "sitemap_id": item.get("sitemap_id"),
        "sitemap_url": item.get("sitemap_url"),
        "sitemap_type": item.get("sitemap_type"),
        "urls_count": item.get("urls_count"),
        "errors_count": item.get("errors_count"),
        "children_count": item.get("children_count"),
        "last_access_date": item.get("last_access_date"),
        "sources": ",".join(item.get("sources") or []),
    } for item in listing]
    write_json(out / "sitemaps.json", payload)
    write_csv(out / "sitemaps.csv", rows, [
        "sitemap_id", "sitemap_url", "sitemap_type", "urls_count",
        "errors_count", "children_count", "last_access_date", "sources",
    ])


def _last_values(payload: dict | None) -> dict[str, float]:
    result = {}
    for name, points in ((payload or {}).get("indicators") or {}).items():
        if points:
            latest = max(points, key=lambda p: str(p.get("date", "")))
            result[name] = latest.get("value")
    return result


def _aggregate_values(payload: dict | None) -> dict[str, float]:
    """За период: TOTAL_* суммируем, позиции (AVG_*) — последнее значение."""
    result = {}
    for name, points in ((payload or {}).get("indicators") or {}).items():
        if not points:
            continue
        if name.startswith("TOTAL_"):
            result[name] = sum(float(p.get("value") or 0) for p in points)
        else:
            latest = max(points, key=lambda p: str(p.get("date", "")))
            result[name] = latest.get("value")
    return result


def build_summary(host: dict, data: dict, errors: dict[str, str], days: int) -> str:
    lines = [
        "# Яндекс.Вебмастер — сводка выгрузки",
        "",
        f"- Сайт: {host.get('unicode_host_url') or host.get('host_id')} "
        f"(`{host.get('host_id')}`)",
        f"- Верифицирован: {host.get('verified')}",
        f"- Период истории: последние {days} дн.",
        f"- Выгружено: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    info = data.get("host_info") or {}
    if info:
        lines += ["## Сайт", ""]
        for key in ("ascii_host_url", "unicode_host_url", "verified", "host_data_status",
                    "host_display_name"):
            if key in info:
                lines.append(f"- {key}: {info[key]}")
        main = info.get("main_mirror") or {}
        if main:
            lines.append(f"- main_mirror: {main.get('host_id')} "
                         f"(verified={main.get('verified')})")
        lines.append("")

    indexing = _last_values(data.get("indexing_history"))
    if indexing:
        lines += ["## Индексация (последние значения)", ""]
        lines += [f"- {name}: {value:g}" if isinstance(value, (int, float))
                  else f"- {name}: {value}" for name, value in sorted(indexing.items())]
        lines.append("")

    popular = data.get("popular_by_shows") or {}
    queries = popular.get("queries", [])
    if queries:
        lines += [f"## Топ-20 запросов по показам "
                  f"({popular.get('date_from')} … {popular.get('date_to')})", "",
                  "| запрос | показы | клики | ср. позиция показа |",
                  "|---|---:|---:|---:|"]
        for query in queries[:20]:
            ind = query.get("indicators") or {}
            lines.append("| {text} | {shows} | {clicks} | {pos} |".format(
                text=str(query.get("query_text", "")).replace("|", "\\|"),
                shows=ind.get("TOTAL_SHOWS", ""),
                clicks=ind.get("TOTAL_CLICKS", ""),
                pos=ind.get("AVG_SHOW_POSITION", ""),
            ))
        lines.append("")

    history = _aggregate_values(data.get("search_queries_history"))
    if history:
        lines += ["## Запросы за период (TOTAL_* — сумма, AVG_* — последнее)", ""]
        lines += [f"- {name}: {value}" for name, value in sorted(history.items())]
        lines.append("")

    problems = ((data.get("diagnostics") or {}).get("problems") or {})
    lines += ["## Диагностика", ""]
    if problems:
        lines += ["| проблема | severity | state | обновлено |", "|---|---|---|---|"]
        for name, problem in sorted(problems.items()):
            lines.append(f"| {name} | {problem.get('severity')} | "
                         f"{problem.get('state')} | {problem.get('last_state_update')} |")
    else:
        lines.append("Проблем нет.")
    lines.append("")

    sitemaps = (data.get("sitemaps") or {}).get("sitemaps") or []
    user_sitemaps = (data.get("user_added_sitemaps") or {}).get("sitemaps") or []
    lines += ["## Sitemap", ""]
    if sitemaps:
        for item in sitemaps:
            lines.append(f"- {item.get('sitemap_url')}: URL {item.get('urls_count')}, "
                         f"ошибок {item.get('errors_count')}, тип {item.get('sitemap_type')}")
    for item in user_sitemaps:
        lines.append(f"- {item.get('sitemap_url')} (добавлен вручную "
                     f"{item.get('added_date')})")
    if not sitemaps and not user_sitemaps:
        lines.append("Sitemap не обнаружены.")
    lines.append("")

    urls = (data.get("important_urls") or {}).get("urls") or []
    lines += ["## Важные страницы", ""]
    if urls:
        bad = [u for u in urls if not (u.get("search_status") or {}).get("searchable")]
        lines.append(f"- всего: {len(urls)}, не в поиске: {len(bad)}")
        for item in bad[:20]:
            status = (item.get("search_status") or {}).get("excluded_url_status")
            lines.append(f"- {item.get('url')} — не в поиске ({status})")
    else:
        lines.append("Важные страницы не отмечены.")
    lines.append("")

    lines += ["## Ошибки выгрузки", ""]
    if errors:
        lines += [f"- {name}: {message}" for name, message in errors.items()]
    else:
        lines.append("Нет.")
    lines.append("")
    return "\n".join(lines)


def save_host_data(out: Path, host: dict, data: dict, errors: dict[str, str],
                   days: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "host_info.json", data.get("host_info") or {})
    if "indexing_history" in data:
        save_indexing_history(out, data["indexing_history"])
    if "search_queries_history" in data:
        save_search_queries_history(out, data["search_queries_history"])
    for key in ("shows", "clicks"):
        payload = data.get(f"popular_by_{key}")
        if payload:
            save_popular(out, key, payload)
    if "diagnostics" in data:
        write_json(out / "diagnostics.json", data["diagnostics"])
    if "important_urls" in data:
        save_important_urls(out, data["important_urls"])
    if "sitemaps" in data:
        save_sitemaps(out, data["sitemaps"])
    if "user_added_sitemaps" in data:
        write_json(out / "user_added_sitemaps.json", data["user_added_sitemaps"])
    write_json(out / "run_meta.json", {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": host,
        "days": days,
        "errors": errors,
    })
    (out / "SUMMARY.md").write_text(build_summary(host, data, errors, days),
                                    encoding="utf-8")


def cmd_auth(args, config: dict[str, str]) -> int:
    if not config.get("YANDEX_CLIENT_ID") or not config.get("YANDEX_CLIENT_SECRET"):
        raise SeoError("В .env.prod нет YANDEX_CLIENT_ID / YANDEX_CLIENT_SECRET.")
    url = (f"{OAUTH_AUTHORIZE}?response_type=code"
           f"&client_id={urllib.parse.quote(config['YANDEX_CLIENT_ID'])}"
           f"&scope={urllib.parse.quote(SCOPE)}")
    print("Откройте ссылку под аккаунтом Яндекса, у которого сайт добавлен в Вебмастер:\n")
    print(url + "\n")
    if args.url_only:
        return 0
    try:
        code = (args.code or input("Код с экрана Яндекса: ")).strip()
    except EOFError:
        raise SeoError("Код не введён (неинтерактивный запуск) — передайте --code.")
    if not code:
        raise SeoError("Пустой код — отменяю.")
    payload = exchange_code(config, code)
    stored = write_token_file(args.token_file, payload)
    print(f"[auth] Токен сохранён: {args.token_file} (chmod 600)")
    print(f"[auth] scope: {stored.get('scope')}")
    print(f"[auth] действует до: "
          f"{datetime.fromtimestamp(stored['expires_at']).isoformat(timespec='minutes')}")
    return 0


def cmd_hosts(args, config: dict[str, str]) -> int:
    token = ensure_access_token(config, args.token_file)
    user_id = get_user_id(token)
    hosts = list_hosts(token, user_id)
    print(f"user_id={user_id}, сайтов: {len(hosts)}\n")
    for host in hosts:
        print(f"- {host.get('host_id')}  verified={host.get('verified')}  "
              f"{host.get('unicode_host_url') or host.get('ascii_host_url')}")
    return 0


def cmd_fetch(args, config: dict[str, str]) -> int:
    token = ensure_access_token(config, args.token_file)
    user_id = get_user_id(token)
    hosts = list_hosts(token, user_id)
    if not hosts:
        raise SeoError("Токену не доступен ни один сайт: добавьте сайт в Вебмастер "
                       "под этим аккаунтом.")
    host = pick_host(hosts, args.host, config.get("WEBSITE_DOMAIN"))
    out = args.out_dir / host_slug(str(host.get("host_id", "host")))
    print(f"[host] {host.get('host_id')} → {out}")
    write_json(out / "hosts_all.json", {"user_id": user_id, "hosts": hosts})

    data, errors = collect_host_data(token, user_id, str(host["host_id"]), args.days)
    save_host_data(out, host, data, errors, args.days)
    if "host_info" not in data:
        raise SeoError("Не удалось прочитать информацию о сайте — см. ошибки выше.")
    print(f"\nГотово: {out / 'SUMMARY.md'}")
    if errors:
        print(f"С ошибками по {len(errors)} ручкам (см. run_meta.json): "
              f"{', '.join(errors)}", file=sys.stderr)
    return 0


def cmd_recrawl(args, config: dict[str, str]) -> int:
    token = ensure_access_token(config, args.token_file)
    user_id = get_user_id(token)
    hosts = list_hosts(token, user_id)
    host = pick_host(hosts, args.host, config.get("WEBSITE_DOMAIN"))
    host_id = str(host["host_id"])
    out = args.out_dir / host_slug(host_id)

    urls: list[str] = list(args.url or [])
    sitemap_url = args.from_sitemap
    if not sitemap_url and not urls:
        base_url = str(host.get("ascii_host_url") or "").rstrip("/")
        if not base_url:
            raise SeoError("У хоста нет ascii_host_url — укажите --from-sitemap или --url.")
        sitemap_url = f"{base_url}/sitemap.xml"
    if sitemap_url and not urls:
        print(f"[sitemap] {sitemap_url}")
        urls = fetch_sitemap_urls(sitemap_url)
        print(f"[sitemap] URL: {len(urls)}")
    if not urls:
        raise SeoError("Нечего отправлять: sitemap пуст, --url не задан.")
    if args.limit:
        urls = urls[: args.limit]

    quota = api_get(token, f"{host_path(user_id, host_id)}/recrawl/quota")
    print(f"[quota] остаток: {quota.get('quota_remainder')} из {quota.get('daily_quota')}")
    if args.dry_run:
        for url in urls:
            print(f"  [dry-run] {url}")
        return 0

    tasks, errors = [], {}
    base = host_path(user_id, host_id)
    for index, url in enumerate(urls, 1):
        try:
            task = api_post(token, f"{base}/recrawl/queue", {"url": url})
            tasks.append({"url": url, **task})
            print(f"[{index}/{len(urls)}] ok {url} (остаток {task.get('quota_remainder')})")
        except ApiError as exc:
            errors[url] = str(exc)
            print(f"[{index}/{len(urls)}] fail {url}: {exc}", file=sys.stderr)
        if index < len(urls) and args.pause:
            time.sleep(args.pause)

    out.mkdir(parents=True, exist_ok=True)
    path = out / "recrawl_tasks.json"
    write_json(path, {
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host_id": host_id,
        "tasks": tasks,
        "errors": errors,
    })
    print(f"\nОтправлено: {len(tasks)}, ошибок: {len(errors)} → {path}")
    return 1 if errors and not tasks else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yandex_webmaster.py",
        description="Яндекс.Вебмастер API v4: OAuth и выгрузка SEO-данных (var/seo/yandex).",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE,
                        help="файл с ключами приложения (по умолчанию .env.prod)")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE,
                        help="где хранить OAuth-токен (chmod 600)")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="получить OAuth-токен по коду из Яндекса")
    auth.add_argument("--code", default=None, help="код верификации (иначе спросит)")
    auth.add_argument("--url-only", action="store_true",
                      help="только напечатать ссылку авторизации")
    auth.set_defaults(func=cmd_auth)

    hosts = sub.add_parser("hosts", help="список сайтов, доступных токену")
    hosts.set_defaults(func=cmd_hosts)

    fetch = sub.add_parser("fetch", help="выгрузить SEO-данные по сайту")
    fetch.add_argument("--host", default=None,
                       help="подстрока host_id/URL (иначе WEBSITE_DOMAIN или единственный)")
    fetch.add_argument("--days", type=int, default=90,
                       help="глубина истории индекс/запросов, дней (по умолчанию 90)")
    fetch.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                       help="каталог выгрузки (по умолчанию var/seo/yandex)")
    fetch.set_defaults(func=cmd_fetch)

    recrawl = sub.add_parser("recrawl", help="поставить URL сайта на переобход")
    recrawl.add_argument("--host", default=None,
                         help="подстрока host_id/URL (иначе WEBSITE_DOMAIN или единственный)")
    recrawl.add_argument("--url", action="append", default=None,
                         help="URL для переобхода (можно несколько раз)")
    recrawl.add_argument("--from-sitemap", default=None,
                         help="URL sitemap.xml (по умолчанию sitemap сайта)")
    recrawl.add_argument("--limit", type=int, default=0,
                         help="взять только первые N URL (0 = все)")
    recrawl.add_argument("--pause", type=float, default=0.4,
                         help="пауза между запросами, сек (по умолчанию 0.4)")
    recrawl.add_argument("--dry-run", action="store_true",
                         help="только показать список URL, ничего не отправлять")
    recrawl.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                         help="каталог выгрузки (по умолчанию var/seo/yandex)")
    recrawl.set_defaults(func=cmd_recrawl)
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
