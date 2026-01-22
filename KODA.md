# KODA.md — Инструкции для работы с проектом Study Agent

## Общая информация о проекте

**Study Agent** — это Telegram-бот для автоматического изучения и конспектирования статей из различных источников. Бот парсит статьи, генерирует краткие конспекты с помощью языковых моделей (LLM) и сохраняет результаты в базу данных SQLite для последующего использования.

### Основные возможности

- Парсинг статей из нескольких источников (habr.com, github.com, infostart.ru)
- Генерация конспектов через локальные (Ollama) и облачные (OpenAI) модели
- Кэширование обработанных статей в SQLite
- Telegram-интерфейс для удобного взаимодействия с ботом

### Технологический стек

| Компонент | Технология |
|-----------|------------|
| Язык программирования | Python 3.10+ |
| Telegram API | pyTelegramBotAPI (telebot) |
| LLM (локальные) | Ollama (gemma3:12b) |
| LLM (облачные) | OpenAI API (gpt-3.5-turbo, gpt-4) |
| Парсинг HTML | BeautifulSoup4, requests |
| База данных | SQLite |
| Конфигурация | python-dotenv |

---

## Структура проекта

```
study_agent/
├── bot.py              # Telegram-бот (точка входа для бота)
├── pipeline.py         # Пайплайн обработки статей (точка входа CLI)
├── scraper.py          # Модуль парсинга статей
├── summarizer.py       # Модуль генерации конспектов
├── database.py         # Модуль работы с SQLite
├── requirements.txt    # Зависимости Python
├── .env                # Конфигурация (токены, API-ключи)
├── data/               # База данных и распарсенные статьи
│   └── study_agent.db  # SQLite база данных
│   └── parsed_articles/# JSON с данными статей
├── conspect/           # Сохранённые конспекты в формате Markdown
└── docs/               # Документация
    ├── add-new-source.md
    ├── add-ollama-model.md
    ├── git-workflow.md
    ├── ROADMAP.md
    └── sources.md
```

---

## Сборка и запуск

### Предварительные требования

1. **Python 3.10+** — проверьте версию: `python --version`
2. **Ollama** (опционально) — для локальных моделей: https://ollama.ai
3. **Telegram-бот** — получите токен у @BotFather

### Установка

```bash
# Клонирование репозитория
git clone https://github.com/FrowningMonk/study_agent.git
cd study_agent

# Создание виртуального окружения
python -m venv venv
# Linux/macOS:
source venv/bin/activate
# Windows:
venv\Scripts\activate

# Установка зависимостей
pip install -r requirements.txt

# Настройка окружения
# Создайте файл .env на основе примера ниже
```

### Конфигурация

Создайте файл `.env` в корне проекта:

```env
# Обязательно: токен Telegram-бота
TELEGRAM_BOT_TOKEN=ваш_токен_от_BotFather

# Опционально: ключ OpenAI API (для облачных моделей)
OPENAI_API_KEY=sk-ваш_ключ_openai
```

### Запуск

**Запуск Telegram-бота:**
```bash
python bot.py
```

**Запуск в CLI-режиме:**
```bash
# С моделью по умолчанию (gemma3:12b)
python pipeline.py https://habr.com/ru/articles/123456/

# С указанием модели
python pipeline.py https://habr.com/ru/articles/123456/ gpt-4

# Интерактивный режим (запрашивает URL и модель)
python pipeline.py
```

**Установка локальной модели Ollama:**
```bash
ollama pull gemma3:12b
```

---

## Архитектура и модули

### Модуль `bot.py`

Telegram-бот с обработчиками команд и сообщений.

**Публичный API:**
```python
from bot import bot, main
main()  # Запуск бота
```

**Ключевые функции:**
- `extract_url(text)` — извлекает URL из текста сообщения
- `is_supported_url(url)` — проверяет поддержку источника
- `get_user_model(user_id)` — возвращает выбранную пользователем модель
- `send_long_message(chat_id, text)` — отправляет длинные сообщения частями

**Обработчики:**
- `/start` — приветствие и инструкции
- `/help` — справка по командам
- `/model` — выбор модели для генерации
- `<URL>` — обработка ссылки и генерация конспекта

### Модуль `pipeline.py`

Центральный пайплайн: URL → Парсинг → Генерация → Сохранение.

**Публичный API:**
```python
from pipeline import process_article, ensure_directories
process_article(url, model='gemma3:12b', save_json=True, user_id=None)
```

**Основные функции:**
- `process_article()` — обрабатывает статью и возвращает путь к конспекту
- `ensure_directories()` — создаёт необходимые директории
- `is_supported_url(url)` — проверяет поддержку URL
- `get_source_name(url)` — определяет источник по URL
- `generate_filename_from_url(url)` — генерирует имя файла из URL

**Поток обработки:**
1. Парсинг контента (`scraper.get_article()`)
2. Сохранение промежуточного JSON (опционально)
3. Генерация конспекта (`summarizer.generate_summary()`)
4. Сохранение конспекта в файл
5. Сохранение/обновление записи в БД

### Модуль `scraper.py`

Парсеры для различных источников контента.

**Публичный API:**
```python
from scraper import get_article
data = get_article('https://habr.com/ru/articles/123456/')
```

**Поддерживаемые источники:**
- `habr.com` — технические статьи с Хабра
- `github.com` — README и документация репозиториев
- `infostart.ru` — статьи по 1С

**Возвращаемый словарь:**
```python
{
    'url': str,           # Канонический URL
    'source': str,        # 'habr' | 'github' | 'infostart'
    'title': str,         # Заголовок статьи/репозитория
    'author': str,        # Автор
    'content': str,       # Основной текст
    'content_length': int, # Длина контента
    # Опционально для GitHub:
    'description': str,
    'stars': str,
    'language': str,
    'files': list[str],
    # Опционально для Хабра:
    'date': str,
}
```

**При ошибке:**
```python
{'error': str}  # Описание ошибки
```

### Модуль `summarizer.py`

Генерация конспектов через LLM.

**Публичный API:**
```python
from summarizer import generate_summary, save_summary_to_file
summary = generate_summary(article_data, model='gemma3:12b')
```

**Доступные модели:**
```python
AVAILABLE_MODELS = {
    'gemma3:12b': 'ollama',      # По умолчанию, локальная
    'gpt-3.5-turbo': 'openai',   # Облачная
    'gpt-4': 'openai',           # Облачная
}
DEFAULT_MODEL = 'gemma3:12b'
```

**Функции:**
- `generate_summary(article_data, model)` — генерирует конспект
- `save_summary_to_file(summary, title, dir)` — сохраняет в файл
- `check_model_availability(model)` — проверяет доступность модели
- `create_prompt(article_data)` — создаёт промпт для статьи

**Особенности:**
- Разные промпты для разных источников
- `check_model_availability()` проверяет наличие модели в Ollama/OpenAI
- Температура генерации: 0.3, max_tokens: 1000

### Модуль `database.py`

Работа с SQLite базой данных.

**Публичный API:**
```python
from database import init_db, article_exists, save_article, get_cached_summary
init_db()  # Инициализация БД (идемпотентная)
```

**Функции:**
- `init_db()` — создаёт таблицы и индексы
- `article_exists(url)` — проверяет наличие статьи
- `get_cached_summary(url)` — возвращает сохранённый конспект
- `save_article(data, summary, model, user_id)` — сохраняет статью
- `update_article(url, summary, model)` — обновляет конспект

**Схема таблицы `articles`:**
```sql
CREATE TABLE articles (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    published_date TEXT,
    github_stars TEXT,
    github_language TEXT,
    github_description TEXT,
    content TEXT NOT NULL,
    summary TEXT,
    model_used TEXT,
    user_id INTEGER,
    processed_at TEXT NOT NULL
);
```

---

## Правила разработки

### Стиль кода

- **Язык документации и комментариев:** русский
- **Типизация:** обязательная (используется `typing`)
- **Именование:**
  - Переменные и функции: `snake_case`
  - Константы: `UPPER_SNAKE_CASE`
  - Классы: `PascalCase`
- **Длина строки:** не более 120 символов
- **Документация модулей:** в начале каждого файла docstring с описанием

### Добавление нового источника

1. Создайте функцию `_parse_<source>(url)` в `scraper.py`
2. Добавьте роутинг в `get_article()` по домену
3. Добавьте источник в `SUPPORTED_SOURCES` в `bot.py` и `pipeline.py`
4. Создайте промпты в `summarizer.py` для нового источника
5. Обновите документацию в `docs/sources.md`

См. подробности: `docs/add-new-source.md`

### Добавление новой модели

1. Добавьте модель в `AVAILABLE_MODELS` в `summarizer.py`
2. Обновите `DEFAULT_MODEL` при необходимости
3. Добавьте проверку в `check_model_availability()`
4. Обновите список моделей в `bot.py` и README.md

См. подробности: `docs/add-ollama-model.md`

### Тестирование

**Текущее состояние:** модульные тесты отсутствуют.

**Рекомендации:**
- Добавить тесты для парсеров (`scraper.py`)
- Добавить тесты для БД (`database.py`)
- Использовать `pytest` как тестовый фреймворк

**Запуск тестов (TODO):**
```bash
pytest tests/
```

### Git-workflow

См. полную инструкцию: `docs/git-workflow.md`

**Основные правила:**
- Ветки именуются по смыслу: `fix-parser-bug`, `add-telegram-bot`
- Сообщения коммитов на русском, с глаголом: "Добавил", "Исправил", "Обновил"
- WIP-коммиты допускаются для промежуточного сохранения
- Pull Request обязателен для merge в main

---

## Команды Telegram-бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие и инструкция |
| `/help` | Справка по использованию |
| `/model` | Выбор модели для генерации |
| `<URL>` | Отправить ссылку → получить конспект |

---

## Директории

| Директория | Назначение |
|------------|------------|
| `data/` | База данных SQLite и JSON с распарсенными статьями |
| `conspect/` | Сохранённые конспекты в формате Markdown |
| `docs/` | Документация по проекту |

---

## Заметки для разработчика

1. **Кэширование:** статьи кэшируются в SQLite. При повторной отправке URL бот предлагает показать сохранённый конспект или сгенерировать заново.

2. **Лимит Telegram:** максимальная длина сообщения — 4096 символов. Длинные конспекты отправляются частями через `send_long_message()`.

3. **Модель по умолчанию:** `gemma3:12b` (Ollama, локальная). Требует запущенного сервера Ollama.

4. **Опциональные зависимости:** OpenAI API используется только если выбрана соответствующая модель и установлен `OPENAI_API_KEY`.

5. **Обработка ошибок:** все модули возвращают словари с полем `error` при неудаче вместо исключений.

6. **Индексы БД:** создаются для `url` и `user_id` для быстрого поиска.

---

## Полезные ссылки

- [Документация Ollama](https://ollama.ai/)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)
- [pyTelegramBotAPI](https://github.com/eternnoir/pyTelegramBotAPI)