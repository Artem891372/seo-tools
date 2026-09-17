# seo-tools

CLI-инструменты для SEO-аналитики из панелей вебмастера и мониторинга конкурентов.
Без фреймворков и зависимостей — только стандартная библиотека Python 3.11+.

| Инструмент | Что выгружает | Авторизация |
|---|---|---|
| [`scripts/seo_gsc.py`](scripts/seo_gsc.py) | Google Search Console: запросы, страницы, страны, устройства; статус URL в индексе | OAuth (Desktop app) |
| [`scripts/seo_bing.py`](scripts/seo_bing.py) | Bing Webmaster: запросы, страницы, краулинг, ошибки | API-ключ |
| [`scripts/yandex_webmaster.py`](scripts/yandex_webmaster.py) | Яндекс.Вебмастер: индексация, поисковые запросы, диагностика, sitemap; переобход URL | OAuth |
| [`scripts/seo_competitors.py`](scripts/seo_competitors.py) | диффы sitemap конкурентов: что появилось, обновилось или исчезло | не нужна |

Каждый инструмент пишет сырые JSON, CSV и человекочитаемый `SUMMARY.md`
в `var/seo/<источник>/`.

## Установка

```bash
git clone https://github.com/Artem891372/seo-tools.git
cd seo-tools
python3 --version   # нужен 3.11+
```

Зависимостей нет: `pip install` не требуется.

## Общие настройки

Ключи берутся из переменных окружения или из файла `.env.prod` в корне репозитория
(путь переопределяется `--env-file`). Общая переменная для всех инструментов —
`WEBSITE_DOMAIN`: если у токена несколько сайтов, по ней выбирается нужный
(иначе первый или указанный через `--site`/`--host`).

| Переменная | Где нужна |
|---|---|
| `WEBSITE_DOMAIN` | все инструменты: выбор сайта, если их несколько |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | `seo_gsc.py` |
| `BING_API_KEY` | `seo_bing.py` |
| `YANDEX_CLIENT_ID`, `YANDEX_CLIENT_SECRET` | `yandex_webmaster.py` |

## Google Search Console

1. console.cloud.google.com → APIs & Services → Library → включить **Google Search Console API**;
2. OAuth consent screen: External, добавьте свой аккаунт в Test users;
3. Credentials → Create credentials → **OAuth client ID** → тип «Desktop app»;
4. `GOOGLE_CLIENT_ID` и `GOOGLE_CLIENT_SECRET` — в `.env.prod`.

```bash
python3 scripts/seo_gsc.py auth          # откроет браузер, поймает код на localhost
python3 scripts/seo_gsc.py sites         # ресурсы, доступные токену
python3 scripts/seo_gsc.py fetch --days 90
python3 scripts/seo_gsc.py inspect --url https://example.com/page
```

Refresh-токен хранится в `var/seo/gsc_token.json` (права 600).

## Bing Webmaster

1. bing.com/webmasters → добавить и подтвердить сайт (можно импортировать из GSC);
2. Settings → API access → принять условия → **Generate API Key**;
3. `BING_API_KEY` — в `.env.prod`.

```bash
python3 scripts/seo_bing.py sites
python3 scripts/seo_bing.py fetch
```

## Яндекс.Вебмастер

1. oauth.yandex.ru → создать приложение, Redirect URI — `https://oauth.yandex.ru/verification_code`;
2. права: `webmaster:hostinfo`, `webmaster:verify`;
3. `YANDEX_CLIENT_ID` и `YANDEX_CLIENT_SECRET` — в `.env.prod`.

```bash
python3 scripts/yandex_webmaster.py auth
python3 scripts/yandex_webmaster.py hosts
python3 scripts/yandex_webmaster.py fetch --days 90
python3 scripts/yandex_webmaster.py recrawl --limit 50 --dry-run
```

`recrawl` берёт URL из sitemap сайта и ставит их на переобход (лимит Вебмастера —
150 URL в день), задачи пишет в `recrawl_tasks.json`. Токен — в
`var/seo/yandex_token.json`.

## Мониторинг конкурентов

Слежение за sitemap: снимок сохраняется в `var/seo/competitors/<имя>.json`,
при следующем запуске показываются новые, обновлённые и удалённые URL.

```bash
python3 scripts/seo_competitors.py \
    --target example=https://example.com/sitemap.xml \
    --target blog=https://example.com/blog/sitemap.xml

# цели из файла ([[имя, url], ...] или {"имя": "url"})
python3 scripts/seo_competitors.py --targets-file targets.json --only example

# тихий режим для cron: только JSON-сводка
python3 scripts/seo_competitors.py --targets-file targets.json --quiet
```

Пример `targets.json`:

```json
[["example", "https://example.com/sitemap.xml"],
 ["blog", "https://example.com/blog/sitemap.xml"]]
```

## Что получается на выходе

```
var/seo/
├── gsc/<ресурс>/          # JSON + CSV + SUMMARY.md
├── bing/<сайт>/           # JSON + CSV + SUMMARY.md
├── yandex/<хост>/         # JSON + CSV + SUMMARY.md + recrawl_tasks.json
└── competitors/<имя>.json # снимки sitemap
```

`SUMMARY.md` — короткая сводка для чтения и для вставки в отчёты: динамика кликов
и показов, топ запросов и страниц, проблемы индексации.

## Безопасность

- ключи и токены не хранятся в репозитории: `.env.prod` и каталог `var/` в `.gitignore`;
- OAuth-токены сохраняются с правами `600` и продлеваются автоматически;
- инструменты только читают данные панелей; запись — единственная операция
  `yandex_webmaster.py recrawl` (ставит URL на переобход) и она есть в `--dry-run`.

## Лицензия

MIT — см. [LICENSE](LICENSE).

Инструменты выросли при работе над сервисом [«Оформитель»](https://оформитель.com) —
оформление учебных работ по ГОСТ из Markdown. Документация:
[github.com/Artem891372/oformitel](https://github.com/Artem891372/oformitel).
