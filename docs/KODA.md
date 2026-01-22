# KODA.md — Инструкционный контекст проекта

## Обзор проекта

**Название:** Агент для изучения ИИ — Telegram-бот для генерации конспектов статей

**Тип проекта:** Программный проект (Python + Telegram Bot API)

**Назначение:** Telegram-бот, который принимает ссылки на статьи из поддерживаемых источников, парсит их содержимое и генерирует краткий конспект с помощью языковых моделей (Ollama или OpenAI).

**Текущий статус:** Прототип с базовой функциональностью (4 из 12 пунктов дорожной карты реализовано)

---

## Архитектура проекта

### Модульная структура

```
├── bot.py           # Telegram-бот (входная точка)
├── pipeline.py      # Единый пайплан обработки
├── scraper.py       # Парсинг статей с разных источников
├── summarizer.py    # Генерация конспектов через LLM
├── requirements.txt # Зависимости проекта
└── docs/            # Документация
    ├── add-new-source.md     # Инструкция по добавлению источников
    ├── add-ollama-model.md   # Инструкция по добавлению моделей
    ├── git-workflow.md       # Работа с Git
    ├── ROADMAP.md            # Дорожная карта
    └── sources.md            # Описание поддерживаемых источников
```

### Модули и их функции

| Модуль | Назначение | Ключевые функции |
|--------|------------|-----------------|
| `bot.py` | Telegram-интерфейс | Обработка команд (`/start`, `/help`, `/model`), извлечение URL из сообщений, отправка длинных сообщений частями |
| `pipeline.py` | Координация пайплайна | `process_article()` — URL → Парсинг → Конспект, сохранение в файлы |
| `scraper.py` | Парсинг источников | `get_article()` — роутер, парсеры для Habr, GitHub, InfoStart |
| `summarizer.py` | Генерация конспектов | `generate_summary()`, `create_prompt()`, поддержка Ollama и OpenAI |

### Поток данных

```
Telegram-сообщение с URL
         ↓
bot.py: extract_url() → валидация источника
         ↓
pipeline.py: process_article()
         ↓
scraper.py: get_article() → парсинг HTML
         ↓
summarizer.py: generate_summary() → LLM (Ollama/OpenAI)
         ↓
Сохранение: data/parsed_articles/*.json + conspect/*.md
         ↓
Отправка конспекта пользователю в Telegram
```

---

## Поддерживаемые источники

| Источник | URL Pattern | Извлекаемые данные |
|----------|-------------|-------------------|
| **Habr.com** | `habr.com/ru/articles/[ID]/` | Заголовок, автор, дата, текст статьи |
| **GitHub.com** | `github.com/[owner]/[repo]` | README, ARCHITECTURE.md, CONTRIBUTING.md и др., звёзды, язык |
| **InfoStart.ru** | `infostart.ru/1c/articles/[ID]/`, `infostart.ru/public/[ID]/` | Заголовок, автор, текст статьи |

---

## Поддерживаемые модели

| Модель | Провайдер | Тип | Статус |
|--------|----------|-----|--------|
| `gemma3:12b` | Ollama | Локальная | По умолчанию |
| `gpt-3.5-turbo` | OpenAI | Облачная | Опционально |
| `gpt-4` | OpenAI | Облачная | Опционально |

---

## Зависимости проекта

```
annotated-types==0.7.0
beautifulsoup4==4.14.3     # Парсинг HTML
httpx==0.28.1               # HTTP-клиент
openai==2.15.0              # OpenAI API
pydantic==2.12.5            # Валидация данных
python-dotenv==1.2.1        # Переменные окружения
pyTelegramBotAPI==4.26.0    # Telegram Bot API
ollama==0.6.1               # Ollama API
```

---

## Сборка и запуск

### Предварительные требования

1. Python 3.10+
2. Telegram-бот (получить токен от @BotFather)
3. Ollama с моделью `gemma3:12b` (для локальной генерации) ИЛИ OpenAI API ключ

### Настройка окружения

Создать файл `.env` в корне проекта:

```bash
# Обязательно
TELEGRAM_BOT_TOKEN=your_telegram_bot_token

# Для OpenAI (опционально)
OPENAI_API_KEY=your_openai_api_key
```

### Запуск Telegram-бота

```bash
python bot.py
```

### Запуск пайплайна из командной строки

```bash
# Интерактивный режим
python pipeline.py

# Обработка конкретной ссылки
python pipeline.py https://habr.com/ru/articles/984968/

# С указанием модели
python pipeline.py https://github.com/anthropics/cookbook gpt-4
```

### Тестирование парсеров

```bash
python scraper.py
```

---

## Правила разработки

### Стиль кодирования

- **Язык:** Python с аннотациями типов
- **Форматирование:** PEP 8
- **Именование:** snake_case для функций и переменных, CamelCase для классов
- **Документация:** docstrings для всех публичных функций
- **Структура:** Модули должны быть независимыми, импортировать только необходимое

### Структура модулей

Каждый модуль должен содержать:
1. Docstring с описанием модуля и поддерживаемых источников/функций
2. Константы в верхней части (после импортов)
3. Приватные функции с префиксом `_` для внутренней логики
4. Публичный API в `__all__`
5. Функцию `main()` для тестирования

### Добавление нового источника

См. `docs/add-new-source.md` — полная пошаговая инструкция.

**Краткий чек-лист:**
1. Добавить функции парсинга в `scraper.py` (`_parse_source()`, `_extract_source_content()`)
2. Обновить роутер `get_article()` в `scraper.py`
3. Добавить промпты в `summarizer.py` (`_create_source_prompt()`)
4. Обновить роутер `create_prompt()` в `summarizer.py`
5. Добавить источник в `SUPPORTED_SOURCES` в `pipeline.py` и `bot.py`
6. Обновить `generate_filename_from_url()` в `pipeline.py`
7. Добавить тестовый URL в `_run_tests()` в `scraper.py`

### Добавление новой модели

См. `docs/add-ollama-model.md`.

**Краткий чек-лист:**
1. Скачать модель: `ollama pull <model_name>`
2. Добавить в `AVAILABLE_MODELS` в `summarizer.py`
3. Добавить в `MODEL_DISPLAY_NAMES` в `bot.py` (опционально)

---

## Структура данных

### Article Data (результат парсинга)

```python
{
    'url': str,           # Исходный URL
    'source': str,        # 'habr' | 'github' | 'infostart'
    'title': str,         # Заголовок статьи/репозитория
    'author': str,        # Автор статьи или владелец репозитория
    'date': str,          # Дата публикации (только для Habr)
    'description': str,   # Описание репозитория (только для GitHub)
    'stars': str,        # Количество звёзд (только для GitHub)
    'language': str,     # Основной язык (только для GitHub)
    'files': list[str],  # Список файлов (только для GitHub)
    'content': str,       # Основной текст (обрезается до 8000 символов)
    'content_length': int,
    'parsed_at': str,    # ISO timestamp (добавляется при сохранении)
}
```

### Константы модулей

| Модуль | Константа | Значение | Описание |
|--------|-----------|----------|----------|
| Все | `SUPPORTED_SOURCES` | Dict[str, str] | Домен → короткое имя |
| scraper | `MAX_CONTENT_LENGTH` | 8000 | Макс. длина контента |
| scraper | `TIMEOUT_SECONDS` | 10 | Таймаут HTTP-запросов |
| bot | `MESSAGE_CHUNK_SIZE` | 4000 | Размер чанка для длинных сообщений |
| summarizer | `DEFAULT_MODEL` | `'gemma3:12b'` | Модель по умолчанию |
| summarizer | `AVAILABLE_MODELS` | Dict[str, str] | Модель → провайдер |

---

## Файлы и их назначение

### Основные файлы

| Файл | Назначение | Ключевые экспорты |
|------|------------|-------------------|
| `bot.py` | Telegram-бот | `main()` — запуск бота |
| `pipeline.py` | Пайплайн обработки | `process_article()`, `ensure_directories()` |
| `scraper.py` | Парсинг | `get_article()` |
| `summarizer.py` | Суммаризация | `generate_summary()`, `check_model_availability()` |

### Выходные директории

| Директория | Содержимое |
|------------|-----------|
| `data/parsed_articles/` | JSON с результатами парсинга |
| `conspect/` | Сгенерированные конспекты (.md) |

---

## Важные примечания

### Ограничения

- Максимальная длина контента для парсинга: 8000 символов (обрезается)
- Максимальная длина конспекта: 1000 токенов
- Сообщения в Telegram разбиваются на части по 4000 символов

### Особенности реализации

- **Парсинг GitHub:** Использует GitHub API для поиска markdown-файлов, объединяет содержимое всех найденных файлов
- **Проверка моделей:** Функция `check_model_availability()` проверяет доступность Ollama и наличие API-ключа OpenAI
- **Очистка контента:** Удаляются скрипты, стили, навигация, комментарии

### TODO и дорожная карта

См. `docs/ROADMAP.md` — текущий прогресс: 4/12 пунктов (33%)

**Реализованные функции:**
- Парсинг Habr, GitHub, InfoStart
- Проверка доступности моделей
- Поддержка Ollama и OpenAI
- Расширенный парсинг GitHub (несколько md-файлов)

**Запланированные функции:**
- Парсинг Arxiv + перевод
- Переход на SQLite
- Система хранения идей и группировки статей
- RAG для поиска по базе знаний

---

## Контакты и поддержка

- Документация: `docs/`
- Дорожная карта: `docs/ROADMAP.md`
- Инструкции по добавлению источников: `docs/add-new-source.md`
- Инструкции по добавлению моделей: `docs/add-ollama-model.md`