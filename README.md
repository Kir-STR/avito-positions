# Avito Position Tracker

Мониторинг позиций объявлений в поисковой выдаче Авито по городам.

## Требования

1. [Python 3.10+](https://www.python.org/downloads/)
2. Установить зависимости:

```bash
python -m pip install playwright
python -m playwright install chromium
```

## Запуск

```bash
python main.py URL
```

`URL` — обязательный аргумент. Поддерживаются два вида ссылок:

**По категории** — переход в конкретную категорию:
```
https://www.avito.ru/all/predlozheniya_uslug/delovye_uslugi/.../razrabotka_chat_botov-ASgBAgIC...
```

**По поисковому запросу** — поиск по тексту внутри категории:
```
https://www.avito.ru/all/predlozheniya_uslug?q=%D0%B8%D0%B8
```

Скрипт вырезает из URL часть после `avito.ru/all/` и подставляет вместо `all` каждый город из списка. Для поисковых ссылок параметр `?q=...` сохраняется.

### Примеры

```bash
# по категории
python main.py "https://www.avito.ru/all/predlozheniya_uslug/delovye_uslugi/..." --debug

# по поисковому запросу
python main.py "https://www.avito.ru/all/predlozheniya_uslug?q=%D0%B8%D0%B8"

# с опциями
python main.py "https://www.avito.ru/all/..." --skip 80 --cities my_cities.txt --keywords my_kw.txt
```

### Все опции

| Опция | Описание |
|---|---|
| `URL` | Ссылка на категорию или поисковый запрос (обязательно) |
| `--skip N` | Пропустить первые N городов |
| `--cities FILE` | Путь к файлу городов (по умолчанию `cities.txt`) |
| `--keywords FILE` | Путь к файлу ключевых слов (по умолчанию `keywords.txt`) |
| `--config FILE` | Путь к config.json (по умолчанию `config.json`) |
| `--debug` | Видимый браузер вместо headless |

## Файлы настроек

### cities.txt

Список городов, по одному на строку (slug из URL Авито):

```
moskva
sankt-peterburg
kazan
# можно комментировать строки
```

### keywords.txt

Ключевые слова для поиска своих объявлений. Слова в одной строке — **И** (все должны быть в заголовке). Разные строки — **ИЛИ**:

```
Ии-ассистент под ключ
чат-бот разработка
```

Здесь объявление считается «моим», если в заголовке есть **все три** слова `Ии-ассистент`, `под`, `ключ` — **или** оба слова `чат-бот`, `разработка`.

### config.json

Настройки тайминга и браузера:

| Параметр | Описание |
|---|---|
| `headless` | `true` — фоновый режим, `false` — видимый браузер |
| `min_delay` / `max_delay` | Задержка между городами (сек) |
| `long_pause_every` | Каждые N городов — длинная пауза |
| `long_pause_min` / `long_pause_max` | Длинная пауза (сек) |
| `page_timeout` | Таймаут загрузки страницы (мс) |
| `selector_timeout` | Таймаут ожидания селектора (мс) |
| `max_retries` | Количество повторов при ошибке |

## Результаты

- `output/results_*.csv` — CSV (UTF-8 с BOM для Excel)
- `output/results_*.json` — JSON
- `logs/run_*.log` — лог выполнения
- `logs/debug_*.html` — HTML страниц при ошибках парсинга
