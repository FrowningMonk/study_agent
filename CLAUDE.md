# CLAUDE.md — Study Agent

## Project Overview

Telegram-бот + CLI для автоматического парсинга статей из фиксированных источников, генерации конспектов с помощью LLM (локальных и облачных) и хранения результатов в SQLite.

## Quick Reference

### Setup
```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
# Создать .env с TELEGRAM_BOT_TOKEN (обязательно) и OPENAI_API_KEY (опционально)
```

### Run
```bash
python bot.py                  # Telegram-бот (основной режим)
python pipeline.py <url>       # CLI: обработка одной статьи
python pipeline.py <url> gpt-4 # CLI: с указанием модели
```

### Test
Автоматических тестов пока нет.

## Architecture

5 модулей Python, ~3100 строк:

| Модуль | Назначение |
|--------|-----------|
| `bot.py` | Telegram-бот: команды, хендлеры, inline-кнопки |
| `pipeline.py` | Пайплайн обработки статей, CLI-точка входа |
| `scraper.py` | Парсеры HTML для каждого источника (Habr, GitHub, Infostart) |
| `summarizer.py` | Генерация конспектов через Ollama/OpenAI |
| `database.py` | SQLite: 3 таблицы (articles, ideas, idea_articles), 18 функций |

### Data Flow
URL → `scraper.get_article()` → `summarizer.generate_summary()` → `database.save_article()` → ответ пользователю

### Key Patterns
- **Router pattern** в `scraper.py`: `get_article()` маршрутизирует к парсерам `_parse_habr()`, `_parse_github()`, `_parse_infostart()`
- **Provider pattern** в `summarizer.py`: `_get_provider()` выбирает Ollama или OpenAI
- **Error dict pattern**: все модули возвращают `dict` с ключом `error` при ошибке, исключения не пробрасываются
- **Hash-based callback**: URL хэшируется для callback_data Telegram (лимит 64 байта)

## Code Conventions

- **Язык комментариев и документации**: русский
- **Именование**: `snake_case` (функции/переменные), `UPPER_SNAKE_CASE` (константы), `PascalCase` (классы)
- **Типизация**: обязательна (модуль `typing`)
- **Макс. длина строки**: 120 символов
- **Структура модуля**: docstring → константы → функции → хендлеры
- **Коммиты**: на русском, глагол + описание ("Добавлено логгирование", "Исправлен парсер")

## Configuration

### Environment Variables (.env)
- `TELEGRAM_BOT_TOKEN` — токен бота (обязательно)
- `OPENAI_API_KEY` — ключ OpenAI (опционально, для gpt-3.5-turbo / gpt-4)

### Models
- `gemma3:12b` — Ollama, локальная, по умолчанию
- `gpt-3.5-turbo` — OpenAI, облачная
- `gpt-4` — OpenAI, облачная

### Content Limits
- Habr/Infostart: макс. 8 000 символов контента
- GitHub: макс. 16 000 символов (несколько .md файлов)
- Telegram: сообщения разбиваются на чанки по 4 000 символов

## Database Schema

- **articles** — спарсенные статьи с конспектами, индексы по `url` (UNIQUE) и `user_id`
- **ideas** — пользовательские идеи/темы, индекс по `user_id`
- **idea_articles** — связь many-to-many, UNIQUE(idea_id, article_id)

## Important Notes

- Директории `data/`, `data/parsed_articles/`, `conspect/` создаются автоматически при первом запуске
- БД инициализируется при импорте `database.py` (`init_db()`)
- Выбор модели пользователем хранится in-memory (`user_models` dict), теряется при перезапуске
- GitHub API без аутентификации: лимит 60 запросов/час
- Промпты для LLM различаются по источнику (Habr — фильтр статей, GitHub — обзор архитектуры, Infostart — ключевые концепции 1С)
