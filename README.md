# Study Agent

Личный Telegram-бот и CLI: присылаешь ссылку на статью — получаешь короткий конспект от LLM, решаешь, стоит ли читать, и складываешь статью в тематические «идеи». Всё хранится в SQLite и потом служит базой знаний для других AI-агентов.

## Возможности

- Парсинг статей с Хабра, GitHub и Инфостарта (ссылка извлекается из любого текста сообщения)
- Конспект через любой облачный API в формате OpenAI (по умолчанию) или через локальную Ollama
- Учёт комментариев к статьям Хабра и Инфостарта: если в обсуждении есть что-то полезное, конспект заканчивается абзацем «В комментариях:»
- Кэш статей в SQLite — повторная ссылка не парсится и не отправляется в LLM
- Идеи (тематические папки): привязка статей и генерация `.md`-описания идеи

## Поддерживаемые источники

| Источник | Что берётся |
|----------|-------------|
| habr.com | Текст статьи + комментарии |
| github.com | README и документация репозитория, звёзды, язык, лицензия, дата последнего push |
| infostart.ru | Текст статьи по 1С, автор, теги + комментарии |

Подробнее — [docs/sources.md](docs/sources.md).

## Установка

```bash
git clone https://github.com/FrowningMonk/study_agent.git
cd study_agent
python -m venv venv
venv\Scripts\activate          # Windows; на Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
```

Скопируйте `.env.example` в `.env` и заполните значения (файл не попадает в git):

| Переменная | Обязательна | Назначение |
|------------|-------------|------------|
| `TELEGRAM_BOT_TOKEN` | да | Токен бота от BotFather |
| `POLZA_API_KEY` | да | Ключ облачного LLM-провайдера с OpenAI-совместимым API |
| `POLZA_BASE_URL` | нет | Адрес этого API (`.../v1`). Сменив его, можно подключить любой OpenAI-совместимый сервис |
| `GITHUB_TOKEN` | нет | Поднимает лимит GitHub API с 60 до 5000 запросов в час |
| `OLLAMA_HOST` | нет | Адрес локального Ollama, если используете его |

Ollama нужна только для локальных моделей: установите [Ollama](https://ollama.ai/) и скачайте модель, например `ollama pull gemma3:12b`.

## Запуск

Telegram-бот:

```bash
python bot.py
```

Бот и сервер используют один токен: перед локальным запуском остановите бота на VPS, иначе Telegram вернёт `409 Conflict`.

CLI — одна ссылка за запуск:

```bash
python pipeline.py <URL> [модель] [провайдер]
python pipeline.py https://habr.com/ru/articles/123456/
python pipeline.py https://github.com/owner/repo gemma3:12b ollama
```

Провайдер: `polza` — облачный OpenAI-совместимый API (по умолчанию) или `ollama`. Модель указывается так, как её называет выбранный сервис; значение по умолчанию задано в `summarizer.py`.

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Приветствие и клавиатура |
| `/help` | Справка |
| `/model` | Выбор провайдера и модели для конспектов и для `.md` идей |
| `/new_idea` | Создать идею |
| `/ideas` | Список идей |
| `/articles` | Список статей |
| `<ссылка>` | Получить конспект |

## Тесты

```bash
pip install -r requirements-dev.txt
pytest
```

Тесты проверяют парсеры (на сохранённых HTML/JSON из `tests/fixtures/`) и сборку промптов; в сеть и к LLM они не ходят.

## Деплой

Сервер — VPS с Docker. Push или merge в `main` запускает GitHub Actions, который обновляет код на сервере и пересобирает контейнер. Секреты сервера лежат в `.env` на VPS и в секретах GitHub Actions, в репозиторий не попадают.

## Структура проекта

```
study_agent/
├── bot.py              # Telegram-бот
├── pipeline.py         # CLI: ссылка → парсинг → конспект → БД
├── scraper.py          # Парсеры источников
├── summarizer.py       # Промпты и вызов LLM (OpenAI-совместимый API / Ollama)
├── database.py         # SQLite
├── tests/              # pytest + фикстуры страниц
├── Dockerfile, docker-compose.yml, scripts/deploy.sh
└── docs/               # Документация
```

## Документация

- [docs/sources.md](docs/sources.md) — источники и что из них извлекается
- [docs/add-new-source.md](docs/add-new-source.md) — как добавить источник
- [docs/git-workflow.md](docs/git-workflow.md) — работа с ветками
- [docs/ROADMAP.md](docs/ROADMAP.md) — планы
