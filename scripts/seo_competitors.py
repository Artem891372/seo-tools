# -*- coding: utf-8 -*-
"""Мониторинг контента конкурентов по публичным sitemap.

Снимает URL+lastmod из sitemap и показывает, что появилось/обновилось
с прошлого запуска. Снимки — var/seo/competitors/<имя>.json.

Запуск:
  python3 scripts/seo_competitors.py \
      --target a4doc=https://a4doc.ai/sitemap.xml \
      --target diplox=https://diplox.online/sitemap.xml

  python3 scripts/seo_competitors.py --targets-file targets.json [--only a4doc]
  python3 scripts/seo_competitors.py --target https://example.com/sitemap.xml --quiet

Файл targets.json (или объект {"имя": "url"}):
  [["a4doc", "https://a4doc.ai/sitemap.xml"],
   ["diplox", "https://diplox.online/sitemap.xml"]]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO / "var" / "seo" / "competitors"


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (SEO-monitor)"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
    return ""


def parse_sitemap(url: str) -> dict[str, str]:
    """URL → lastmod. Поддерживает только urlset; порядок тегов внутри <url> любой."""
    text = fetch_text(url)
    result: dict[str, str] = {}
    for block in re.findall(r"<url>(.*?)</url>", text, re.S):
        loc = re.search(r"<loc>([^<]+)</loc>", block)
        if not loc:
            continue
        mod = re.search(r"<lastmod>([^<]+)</lastmod>", block)
        result[loc.group(1).strip()] = mod.group(1).strip() if mod else ""
    if not result:
        return {loc: "" for loc in re.findall(r"<loc>([^<]+)</loc>", text)}
    return result


def section(url: str) -> str:
    path = re.sub(r"^https?://[^/]+", "", url).strip("/")
    return path.split("/")[0] if path else "(корень)"


def parse_target(value: str) -> tuple[str, str]:
    """`NAME=URL` или просто `URL` (имя — домен) → (имя, url)."""
    if "=" in value:
        name, _, url = value.partition("=")
        name, url = name.strip(), url.strip()
    else:
        url = value.strip()
        name = re.sub(r"^https?://", "", url).split("/")[0]
    if not name or not url.startswith(("http://", "https://")):
        raise argparse.ArgumentTypeError(f"ожидается NAME=URL или URL, получено {value!r}")
    return name, url


def load_targets_file(path: Path) -> list[tuple[str, str]]:
    """JSON-файл: [[имя, url], ...] или {"имя": "url"}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = list(data.items())
    return [(str(name), str(url)) for name, url in data]


def report(name: str, current: dict[str, str], previous: dict[str, str] | None,
           quiet: bool) -> dict:
    previous = previous or {}
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    updated = sorted(u for u in set(current) & set(previous)
                     if current[u] and current[u] != previous.get(u))
    entry = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": len(current),
        "added": added, "removed": removed, "updated": updated,
    }
    if quiet:
        return entry
    print(f"\n### {name}: всего URL {len(current)}")
    if previous:
        print(f"новых: {len(added)} | удалено: {len(removed)} | обновлено: {len(updated)}")
    else:
        print("первый снимок — диффы появятся при следующем запуске")
    if current:
        print("секции:", dict(Counter(section(u) for u in current).most_common(8)))
    for label, urls in (("NEW", added), ("UPD", updated), ("DEL", removed)):
        for url in urls[:15]:
            suffix = f"  [{current.get(url) or previous.get(url, '')[:10]}]" if label != "DEL" else ""
            print(f"  {label} {url}{suffix}")
        if len(urls) > 15:
            print(f"  … ещё {len(urls) - 15}")
    return entry


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Диффы sitemap конкурентов.")
    parser.add_argument("--target", action="append", default=[], type=parse_target,
                        metavar="NAME=URL",
                        help="sitemap для слежения, можно несколько раз")
    parser.add_argument("--targets-file", type=Path, default=None,
                        help="JSON: [[имя, url], ...] или {\"имя\": \"url\"}")
    parser.add_argument("--only", action="append", default=None,
                        help="имя цели из списка (можно несколько раз)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--quiet", action="store_true", help="без вывода, только снимки")
    args = parser.parse_args(argv)

    targets: list[tuple[str, str]] = []
    if args.targets_file:
        try:
            targets += load_targets_file(args.targets_file)
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: не читается {args.targets_file}: {exc}", file=sys.stderr)
            return 1
    targets += args.target
    if not targets:
        print("error: задайте цели: --target NAME=URL (можно несколько) "
              "или --targets-file targets.json", file=sys.stderr)
        return 2

    if args.only:
        wanted = set(args.only)
        targets = [t for t in targets if t[0] in wanted]
        if not targets:
            print(f"error: нет целей {sorted(wanted)}", file=sys.stderr)
            return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary = {}
    for name, sitemap in targets:
        snapshot_path = args.out_dir / f"{name}.json"
        previous = None
        if snapshot_path.is_file():
            previous = json.loads(snapshot_path.read_text(encoding="utf-8")).get("urls")
        try:
            current = parse_sitemap(sitemap)
        except Exception as exc:
            print(f"error: {name}: {exc}", file=sys.stderr)
            continue
        entry = report(name, current, previous, args.quiet)
        snapshot_path.write_text(json.dumps(
            {"fetched_at": entry["fetched_at"], "urls": current},
            ensure_ascii=False, indent=1), encoding="utf-8")
        summary[name] = entry
    if args.quiet:
        print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "urls"}
                          for k, v in summary.items()}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
