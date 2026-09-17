# -*- coding: utf-8 -*-
"""Bing Webmaster API (API key): выгрузка SEO-данных в var/seo/bing.

Разовый CLI-инструмент по образцу scripts/yandex_webmaster.py: без инфраструктуры,
только стандартная библиотека 3.11+.

Подготовка ключа (один раз):
  1. bing.com/webmasters → добавить и подтвердить сайт (можно импортировать из GSC).
  2. Settings → API access → принять условия → Generate API Key.
  3. BING_API_KEY=... → .env.prod.

Запуск (из корня репо):
  python3 scripts/seo_bing.py sites
  python3 scripts/seo_bing.py fetch [--site <подстрока>]
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
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

API_BASE = "https://ssl.bing.com/webmaster/api.svc/json"
DEFAULT_ENV_FILE = REPO / ".env.prod"
DEFAULT_OUT_DIR = REPO / "var" / "seo" / "bing"

DATE_RE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")
DATE_FIELDS = {"Date", "LastCrawled", "DiscoveryDate", "LastSeen"}


class SeoError(RuntimeError):
    """Ошибка с человекочитаемой подсказкой (печатается без трейсбека)."""


class ApiError(SeoError):
    """Ошибка Bing Webmaster API: HTTP-код + ErrorCode/Message."""

    def __init__(self, status: int, code, message: str, method: str):
        self.status, self.code, self.message, self.method = status, code, message, method
        super().__init__(f"HTTP {status} {code} на {method}: {message}")


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
    for key in ("BING_API_KEY", "WEBSITE_DOMAIN"):
        if os.environ.get(key):
            merged[key] = os.environ[key]
    return merged


def _safe_json(blob: bytes) -> dict:
    try:
        data = json.loads(blob.decode("utf-8"))
        return data if isinstance(data, dict) else {"d": data}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _convert_dates(value):
    """WCF-даты /Date(ms)/ → ISO; служебные __type выкидываем; рекурсивно."""
    if isinstance(value, dict):
        return {k: (_iso_date(v) if k in DATE_FIELDS else _convert_dates(v))
                for k, v in value.items() if not str(k).startswith("__")}
    if isinstance(value, list):
        return [_convert_dates(v) for v in value]
    return value


def _iso_date(value):
    if isinstance(value, str):
        match = DATE_RE.match(value)
        if match:
            dt = datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
            return dt.date().isoformat()
    return value


def bing_get(api_key: str, method: str, params: dict | None = None,
             retries: int = 3, timeout: int = 60) -> list | dict:
    """GET метода Bing API; возвращает распакованное поле d, даты — в ISO."""
    query = {"apikey": api_key, **(params or {})}
    url = f"{API_BASE}/{method}?{urllib.parse.urlencode(query)}"
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"Accept": "application/json"}),
                    timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8") or "{}")
                return _convert_dates(payload.get("d", payload))
        except urllib.error.HTTPError as exc:
            payload = _safe_json(exc.read())
            if exc.code in (429, 500, 502, 503) and attempt < retries:
                delay = int(exc.headers.get("Retry-After") or 2 ** attempt)
                print(f"[api] HTTP {exc.code}, повтор через {delay} с …")
                time.sleep(delay)
                continue
            raise ApiError(exc.code, payload.get("ErrorCode", exc.code),
                           str(payload.get("Message", "")), method) from exc
    raise ApiError(0, "RETRIES_EXHAUSTED", "исчерпаны повторы", method)


def list_sites(api_key: str) -> list[dict]:
    """GetUserSites — актуальный метод; GetSites оставлен фолбэком для старых ключей."""
    try:
        result = bing_get(api_key, "GetUserSites")
    except ApiError as exc:
        if exc.status != 404:
            raise
        result = bing_get(api_key, "GetSites")
    return list(result if isinstance(result, list) else [])


def site_url(site: dict) -> str:
    return str(site.get("Url") or site.get("url") or "")


def pick_site(sites: list[dict], needle: str | None, website_domain: str | None) -> dict:
    def matches(site: dict, token: str) -> bool:
        return token.lower() in site_url(site).lower()

    if needle:
        found = [s for s in sites if matches(s, needle)]
        if len(found) != 1:
            raise SeoError(f"--site {needle!r}: совпадений {len(found)}. "
                           f"Доступные: {', '.join(site_url(s) for s in sites)}")
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
    raise SeoError("Не понимаю, какой сайт брать: подходящих несколько. "
                   "Укажите --site. Доступные: " + ", ".join(site_url(s) for s in sites))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def collect_site_data(api_key: str, url: str) -> tuple[dict, dict[str, str]]:
    """Собрать все срезы по сайту; ошибки отдельных методов не роняют выгрузку."""
    data: dict = {}
    errors: dict[str, str] = {}

    def attempt(name: str, method: str):
        try:
            data[name] = bing_get(api_key, method, {"siteUrl": url})
            print(f"[ok]   {name}")
        except ApiError as exc:
            errors[name] = str(exc)
            print(f"[fail] {name}: {exc}", file=sys.stderr)

    attempt("rank_and_traffic", "GetRankAndTrafficStats")
    attempt("query_stats", "GetQueryStats")
    attempt("page_stats", "GetPageStats")
    attempt("crawl_stats", "GetCrawlStats")
    attempt("crawl_issues", "GetCrawlIssues")
    attempt("feeds", "GetFeeds")
    return data, errors


def _rows_of(payload) -> list[dict]:
    return [row for row in payload or [] if isinstance(row, dict)]


def build_summary(site: dict, data: dict, errors: dict[str, str]) -> str:
    url = site_url(site)
    lines = [
        "# Bing Webmaster — сводка выгрузки", "",
        f"- Сайт: {url}",
        f"- Верифицирован: {site.get('IsVerified')}",
        f"- Выгружено: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
    ]

    traffic = _rows_of(data.get("rank_and_traffic"))
    if traffic:
        impressions = sum(row.get("Impressions") or 0 for row in traffic)
        clicks = sum(row.get("Clicks") or 0 for row in traffic)
        dates = [str(row.get("Date") or "") for row in traffic]
        lines += ["## Трафик (весь доступный период)", "",
                  f"- Показы: {impressions:g}",
                  f"- Клики: {clicks:g}",
                  f"- Период: {min(d for d in dates if d)} … {max(d for d in dates if d)}", ""]
    else:
        lines += ["## Трафик", "", "Данных нет.", ""]

    queries = _rows_of(data.get("query_stats"))
    if queries:
        lines += ["## Топ-20 запросов", "",
                  "| запрос | показы | клики | ср. позиция |", "|---|---:|---:|---:|"]
        for row in queries[:20]:
            lines.append("| {q} | {i} | {c} | {p} |".format(
                q=str(row.get("Query") or row.get("query") or "").replace("|", "\\|"),
                i=row.get("Impressions") or 0, c=row.get("Clicks") or 0,
                p=row.get("AvgImpressionPosition") or row.get("AvgClickPosition") or ""))
        lines.append("")

    pages = _rows_of(data.get("page_stats"))
    if pages:
        lines += ["## Топ-20 страниц", "",
                  "| страница | показы | клики |", "|---|---:|---:|"]
        for row in pages[:20]:
            lines.append("| {u} | {i} | {c} |".format(
                u=str(row.get("Url") or row.get("Page") or "").replace("|", "\\|"),
                i=row.get("Impressions") or 0, c=row.get("Clicks") or 0))
        lines.append("")

    issues = _rows_of(data.get("crawl_issues"))
    lines += ["## Ошибки сканирования", ""]
    if issues:
        by_type: dict[str, int] = {}
        for row in issues:
            key = str(row.get("IssueType") or row.get("issueType") or "unknown")
            by_type[key] = by_type.get(key, 0) + 1
        lines += [f"- {name}: {count}" for name, count in sorted(by_type.items())]
    else:
        lines.append("Ошибок нет.")
    lines.append("")

    feeds = _rows_of(data.get("feeds"))
    lines += ["## Sitemap / feeds", ""]
    lines += ([f"- {row.get('Url') or row.get('url')}" for row in feeds] or
              ["Не обнаружены."])
    lines.append("")

    lines += ["## Ошибки выгрузки", ""]
    lines += [f"- {name}: {message}" for name, message in errors.items()] or ["Нет."]
    lines.append("")
    return "\n".join(lines)


def save_site_data(out: Path, site: dict, data: dict, errors: dict[str, str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, payload in data.items():
        write_json(out / f"{name}.json", payload)
        write_csv(out / f"{name}.csv", _rows_of(payload))
    write_json(out / "run_meta.json", {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site": site, "errors": errors,
    })
    (out / "SUMMARY.md").write_text(build_summary(site, data, errors), encoding="utf-8")


def cmd_sites(args, config: dict[str, str]) -> int:
    api_key = args.api_key or config.get("BING_API_KEY")
    if not api_key:
        raise SeoError("Нет BING_API_KEY (в .env.prod или --api-key).")
    sites = list_sites(api_key)
    print(f"сайтов: {len(sites)}\n")
    for site in sites:
        print(f"- {site_url(site)}  verified={site.get('IsVerified')}")
    return 0


def cmd_fetch(args, config: dict[str, str]) -> int:
    api_key = args.api_key or config.get("BING_API_KEY")
    if not api_key:
        raise SeoError("Нет BING_API_KEY (в .env.prod или --api-key).")
    sites = list_sites(api_key)
    if not sites:
        raise SeoError("К ключу не привязан ни один сайт: добавьте сайт в Bing Webmaster.")
    site = pick_site(sites, args.site, config.get("WEBSITE_DOMAIN"))
    url = site_url(site)
    slug = re.sub(r"[^0-9A-Za-z._-]+", "_", url).strip("_") or "site"
    out = args.out_dir / slug
    print(f"[site] {url} → {out}")
    write_json(out / "sites_all.json", {"sites": sites})

    data, errors = collect_site_data(api_key, url)
    save_site_data(out, site, data, errors)
    print(f"\nГотово: {out / 'SUMMARY.md'}")
    if errors:
        print(f"С ошибками по {len(errors)} методам (см. run_meta.json): "
              f"{', '.join(errors)}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seo_bing.py",
        description="Bing Webmaster API: выгрузка SEO-данных (var/seo/bing).",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--api-key", default=None,
                        help="ключ API (иначе BING_API_KEY из окружения/.env.prod)")
    sub = parser.add_subparsers(dest="command", required=True)

    sites = sub.add_parser("sites", help="список сайтов в Bing Webmaster")
    sites.set_defaults(func=cmd_sites)

    fetch = sub.add_parser("fetch", help="выгрузить данные по сайту")
    fetch.add_argument("--site", default=None,
                       help="подстрока URL (иначе WEBSITE_DOMAIN или единственный)")
    fetch.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    fetch.set_defaults(func=cmd_fetch)
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
