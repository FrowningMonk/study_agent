"""
Модуль генерации конспектов через Polza.ai и Ollama.

Поддерживает разные промпты для разных источников:
    - habr.com — статьи (технические, аналитические)
    - github.com — README репозиториев
    - infostart.ru — статьи и публикации по 1С

Поддерживаемые провайдеры:
    - polza — Polza.ai API (любая модель, основной провайдер)
    - ollama — локальные модели (резерв для локальной разработки)

Example:
    >>> from summarizer import generate_summary
    >>> summary = generate_summary(article_data, model='gemma3:12b', provider='ollama')
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

import ollama
from dotenv import load_dotenv
from openai import OpenAI

# Загружаем переменные окружения
load_dotenv(override=True)

POLZA_API_KEY: str | None = os.getenv('POLZA_API_KEY')

# Инициализируем клиент Polza.ai (OpenAI-совместимый API).
# Заглушка вместо None обязательна: иначе SDK молча подставит OPENAI_API_KEY
# и отправит чужой ключ на сервер Polza.
polza_client = OpenAI(
    api_key=POLZA_API_KEY or 'not-set',
    base_url=os.getenv('POLZA_BASE_URL', 'https://api.polza.ai/v1'),
)

# Публичный API модуля
__all__ = [
    'generate_summary',
    'check_model_availability',
    'check_providers_status',
    'SUPPORTED_PROVIDERS',
    'DEFAULT_MODEL',
    'DEFAULT_PROVIDER',
    'DEFAULT_MD_MODEL',
    'DEFAULT_MD_PROVIDER',
    'generate_idea_md',
    'revise_idea_md',
]


# =============================================================================
# КОНФИГУРАЦИЯ МОДЕЛЕЙ
# =============================================================================

# Поддерживаемые провайдеры
SUPPORTED_PROVIDERS: list[str] = ['polza', 'ollama']

# Модель и провайдер по умолчанию (для конспектов)
DEFAULT_PROVIDER: str = 'polza'
DEFAULT_MODEL: str = 'deepseek/deepseek-v4.1-flash-1017'

# Модель и провайдер по умолчанию (для генерации .md)
DEFAULT_MD_PROVIDER: str = 'polza'
DEFAULT_MD_MODEL: str = 'deepseek/deepseek-v4.1-flash-1017'

# Лимиты генерации (общие для всех провайдеров)
MAX_OUTPUT_TOKENS: int = 4000
GENERATION_TIMEOUT_SECONDS: int = 120
# Контекст Ollama по умолчанию 2–8k токенов: длинная статья (до 60k символов)
# молча обрезалась бы вместе с инструкциями
OLLAMA_NUM_CTX: int = 32768


# =============================================================================
# ПРОМПТЫ ДЛЯ ХАБРА
# =============================================================================

HABR_SYSTEM_PROMPT: str = """Ты — фильтр статей для занятого разработчика.

Твоя задача: помочь понять, стоит ли читать статью.

Правила:
- Пиши простым текстом без форматирования
- Никаких списков, заголовков, эмодзи
- Максимум 5-7 предложений
- Передай суть и тон статьи (автор хвалится, жалуется, учит, делится опытом?)
- В конце — один главный вывод автора
- Если ниже статьи даны комментарии и в них есть ценное (поправки, критика, альтернативные решения, ответы автора, практический опыт) — добавь в конце отдельный абзац, начинающийся со слов «В комментариях:», 1-3 предложения. Если ценного нет или комментариев нет — этот абзац не пиши.
"""

HABR_USER_PROMPT_TEMPLATE: str = """Перескажи суть статьи в 2-3 предложениях: кто автор, что сделал, каков результат. Потом одно предложение — главный вывод.

ЗАГОЛОВОК: {title}
АВТОР: {author}
ДАТА: {date}

ТЕКСТ СТАТЬИ:
{content}
{comments}"""


# =============================================================================
# ПРОМПТЫ ДЛЯ INFOSTART
# =============================================================================

INFOSTART_SYSTEM_PROMPT: str = """Ты — помощник разработчика 1С.

Твоя задача: помочь быстро понять, стоит ли читать статью по 1С.

Правила:
- Пиши простым текстом без форматирования
- Никаких списков, заголовков, эмодзи
- Максимум 5-7 предложений
- Передай суть: какую проблему решает автор, какой подход использует
- В конце — главный практический вывод или рекомендацию
- Если ниже статьи даны комментарии и в них есть ценное (поправки, критика, альтернативные решения, ответы автора, практический опыт) — добавь в конце отдельный абзац, начинающийся со слов «В комментариях:», 1-3 предложения. Если ценного нет или комментариев нет — этот абзац не пиши.
"""

INFOSTART_USER_PROMPT_TEMPLATE: str = """Перескажи суть статьи по 1С в 2-3 предложениях: какая задача решается, какой подход использован, какой результат. Потом одно предложение — главный практический вывод.

ЗАГОЛОВОК: {title}
АВТОР: {author}

ТЕКСТ СТАТЬИ:
{content}
{comments}"""


# =============================================================================
# ПРОМПТЫ ДЛЯ GITHUB
# =============================================================================

GITHUB_SYSTEM_PROMPT: str = """Ты — аналитик open-source проектов.

Твоя задача: дать краткую справку о репозитории на основе всех доступных файлов документации.

Правила:
- Пиши кратко, без эмодзи
- Три пункта: Назначение, Технологии, Зрелость
- Зрелость оценивай по звёздам и полноте документации
- Учитывай информацию из всех предоставленных файлов (README, ARCHITECTURE, CONTRIBUTING и др.)
"""

GITHUB_USER_PROMPT_TEMPLATE: str = """Дай справку о репозитории по трём пунктам:
- Назначение (что делает, для кого)
- Технологии (языки, фреймворки, зависимости)
- Зрелость (оценка по звёздам, документации, активности)

РЕПОЗИТОРИЙ: {title}
АВТОР: {author}
ОПИСАНИЕ: {description}
ЗВЁЗДЫ: {stars}
ЯЗЫК: {language}
ФАЙЛЫ ДОКУМЕНТАЦИИ: {files}

СОДЕРЖИМОЕ:
{content}
"""


# =============================================================================
# ПРОМПТЫ ДЛЯ ГЕНЕРАЦИИ .MD ИДЕЙ
# =============================================================================

IDEA_MD_SYSTEM_PROMPT: str = """Ты -- технический писатель. Создай структурированное .md-описание идеи (темы исследования).
Документ будет использоваться как контекст для LLM и агентов.

Структура:
1. Заголовок -- название идеи
2. Описание -- суть в 2-3 предложениях
3. Ключевые концепции -- основные понятия и термины
4. Открытые вопросы -- что стоит изучить
5. Возможные направления -- как развивать тему

Пиши на русском, будь конкретен, опирайся только на предоставленное описание."""

IDEA_MD_USER_PROMPT_TEMPLATE: str = """Создай .md-описание для идеи.

НАЗВАНИЕ: {idea_name}
ОПИСАНИЕ: {idea_description}"""

IDEA_MD_REVISE_SYSTEM_PROMPT: str = """Переработай .md-описание идеи по замечаниям. Сохрани структуру."""

IDEA_MD_REVISE_USER_PROMPT_TEMPLATE: str = """Документ:
{current_md}

Замечания:
{feedback}

Верни полный обновленный документ."""


# =============================================================================
# ФУНКЦИИ РАБОТЫ С ПРОМПТАМИ
# =============================================================================


COMMENTS_MAX_CHARS: int = 12000
COMMENT_MAX_CHARS: int = 1500


def _select_comments(comments: list[dict], budget: int) -> list[dict]:
    """
    Отбирает комментарии в пределах бюджета символов.

    Приоритет: ответы автора статьи, затем по рейтингу (если есть), затем по порядку.
    Комментарии с отрицательным рейтингом отбрасываются. Результат — в исходном порядке.
    """
    candidates = [c for c in comments if (c.get('score') or 0) >= 0]
    ranked = sorted(
        candidates,
        key=lambda c: (not c.get('is_post_author'), -(c.get('score') or 0), c['number']),
    )
    chosen, used = set(), 0
    for c in ranked:
        size = min(len(c['text']), COMMENT_MAX_CHARS)
        if used + size > budget:
            continue
        chosen.add(c['number'])
        used += size
    return [c for c in candidates if c['number'] in chosen]


def format_comments(comments: list[dict] | None, budget: int = COMMENTS_MAX_CHARS) -> str:
    """
    Форматирует комментарии для промпта.

    Строка вида «#3 → #2 alias [автор статьи] (+5): текст».
    Пустая строка, если комментариев нет.
    """
    if not comments:
        return ''

    selected = _select_comments(comments, budget)
    if not selected:
        return ''

    lines = []
    for c in selected:
        head = f"#{c['number']}"
        if c.get('reply_to'):
            head += f" → #{c['reply_to']}"
        head += f" {c['author']}"
        if c.get('is_post_author'):
            head += ' [автор статьи]'
        if c.get('score') is not None:
            head += f" ({c['score']:+d})"
        text = c['text']
        if len(text) > COMMENT_MAX_CHARS:
            text = text[:COMMENT_MAX_CHARS] + '...'
        lines.append(f'{head}: {text}')

    header = f'\nКОММЕНТАРИИ (показано {len(selected)} из {len(comments)}):\n'
    return header + '\n'.join(lines) + '\n'


def _create_habr_prompt(article_data: dict) -> tuple[str, str]:
    """
    Создаёт промпт для статьи с Хабра.

    Args:
        article_data: Словарь с данными статьи.

    Returns:
        Кортеж (system_prompt, user_prompt).
    """
    user_prompt = HABR_USER_PROMPT_TEMPLATE.format(
        title=article_data.get('title', 'Не указан'),
        author=article_data.get('author', 'Не указан'),
        date=article_data.get('date', 'Не указана'),
        content=article_data.get('content', 'Текст отсутствует'),
        comments=format_comments(article_data.get('comments')),
    )
    return HABR_SYSTEM_PROMPT, user_prompt


def _create_infostart_prompt(article_data: dict) -> tuple[str, str]:
    """
    Создаёт промпт для статьи с InfoStart.

    Args:
        article_data: Словарь с данными статьи.

    Returns:
        Кортеж (system_prompt, user_prompt).
    """
    user_prompt = INFOSTART_USER_PROMPT_TEMPLATE.format(
        title=article_data.get('title', 'Не указан'),
        author=article_data.get('author', 'Не указан'),
        content=article_data.get('content', 'Текст отсутствует'),
        comments=format_comments(article_data.get('comments')),
    )
    return INFOSTART_SYSTEM_PROMPT, user_prompt


def _create_github_prompt(article_data: dict) -> tuple[str, str]:
    """
    Создаёт промпт для документации с GitHub.

    Args:
        article_data: Словарь с данными репозитория.

    Returns:
        Кортеж (system_prompt, user_prompt).
    """
    # Формируем список файлов
    files = article_data.get('files', ['README.md'])
    files_str = ', '.join(files) if isinstance(files, list) else 'README.md'

    user_prompt = GITHUB_USER_PROMPT_TEMPLATE.format(
        title=article_data.get('title', 'Не указан'),
        author=article_data.get('author', 'Не указан'),
        description=article_data.get('description', 'Нет описания'),
        stars=article_data.get('stars', '0'),
        language=article_data.get('language', 'Не определён'),
        files=files_str,
        content=article_data.get('content', 'Документация отсутствует'),
    )
    return GITHUB_SYSTEM_PROMPT, user_prompt


def create_prompt(article_data: dict) -> tuple[str, str]:
    """
    Роутер: выбирает промпт в зависимости от источника.

    Args:
        article_data: Словарь с данными (обязательное поле 'source').

    Returns:
        Кортеж (system_prompt, user_prompt).

    Raises:
        ValueError: Если источник не поддерживается.
    """
    source = article_data.get('source', 'unknown')

    if source == 'habr':
        return _create_habr_prompt(article_data)
    elif source == 'github':
        return _create_github_prompt(article_data)
    elif source == 'infostart':
        return _create_infostart_prompt(article_data)
    else:
        raise ValueError(f'Неподдерживаемый источник: {source}')


# =============================================================================
# ГЕНЕРАЦИЯ ЧЕРЕЗ OLLAMA
# =============================================================================


def _generate_with_ollama(
    system_prompt: str,
    user_prompt: str,
    model: str,
) -> str:
    """
    Генерирует текст через локальную модель Ollama.

    Args:
        system_prompt: Системный промпт.
        user_prompt: Пользовательский промпт.
        model: Название модели в Ollama.

    Returns:
        Сгенерированный текст.

    Raises:
        Exception: При ошибке соединения или генерации.
    """
    context_length = len(system_prompt) + len(user_prompt)
    logger.info('Отправляю запрос в Ollama: model=%s, context_length=%d', model, context_length)

    response = ollama.chat(
        model=model,
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        options={
            'temperature': 0.3,
            'num_predict': MAX_OUTPUT_TOKENS,  # аналог max_tokens
            'num_ctx': OLLAMA_NUM_CTX,
        },
    )

    logger.info('Ответ от Ollama получен: model=%s, response_length=%d', model, len(response['message']['content']))
    return response['message']['content']


# =============================================================================
# ГЕНЕРАЦИЯ ЧЕРЕЗ POLZA.AI
# =============================================================================


def _generate_with_polza(
    system_prompt: str,
    user_prompt: str,
    model: str,
) -> str:
    """
    Генерирует текст через Polza.ai API (OpenAI-совместимый).

    Args:
        system_prompt: Системный промпт.
        user_prompt: Пользовательский промпт.
        model: Название модели в Polza.ai.

    Returns:
        Сгенерированный текст.

    Raises:
        RuntimeError: Если POLZA_API_KEY не задан.
        Exception: При ошибке API.
    """
    if not POLZA_API_KEY:
        raise RuntimeError('POLZA_API_KEY не задан')

    response = polza_client.chat.completions.create(
        model=model,
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        temperature=0.3,
        max_tokens=MAX_OUTPUT_TOKENS,
        timeout=GENERATION_TIMEOUT_SECONDS,
    )

    return response.choices[0].message.content


# =============================================================================
# ДИСПЕТЧЕР ПРОВАЙДЕРОВ
# =============================================================================


def _generate(
    system_prompt: str,
    user_prompt: str,
    model: str,
    provider: str,
) -> str:
    """
    Диспетчер: вызывает генерацию через нужного провайдера.

    Args:
        system_prompt: Системный промпт.
        user_prompt: Пользовательский промпт.
        model: Название модели.
        provider: Провайдер ('polza', 'ollama').

    Returns:
        Сгенерированный текст.

    Raises:
        ValueError: Если провайдер не поддерживается.
    """
    if provider == 'ollama':
        return _generate_with_ollama(system_prompt, user_prompt, model)
    elif provider == 'polza':
        return _generate_with_polza(system_prompt, user_prompt, model)
    else:
        raise ValueError(f'Неподдерживаемый провайдер: {provider}')


# =============================================================================
# ПРОВЕРКА ДОСТУПНОСТИ МОДЕЛЕЙ
# =============================================================================


def check_model_availability(model: str, provider: str) -> tuple[bool, str | None]:
    """
    Проверяет доступность модели у указанного провайдера.

    Args:
        model: Название модели.
        provider: Провайдер ('polza', 'ollama').

    Returns:
        Кортеж (доступна ли модель, сообщение об ошибке или None).
    """
    try:
        if provider == 'ollama':
            try:
                models = ollama.list()
                available_models = [m.model for m in models.get('models', [])]
                if model not in available_models:
                    return False, (
                        f'Модель {model} не найдена в Ollama. '
                        f'Доступные: {", ".join(available_models) if available_models else "нет"}'
                    )
                return True, None
            except Exception as e:
                error_msg = str(e).lower()
                if 'connection' in error_msg or 'connect' in error_msg:
                    return False, 'Ollama не запущена. Запустите команду: ollama serve'
                elif 'keyerror' in error_msg or "'name'" in error_msg:
                    return False, 'Ошибка версии библиотеки ollama. Обновите: pip install --upgrade ollama'
                return False, 'Не удалось подключиться к Ollama. Проверьте, что сервис запущен (ollama serve)'

        elif provider == 'polza':
            if not POLZA_API_KEY:
                return False, 'API ключ Polza.ai не найден. Добавьте POLZA_API_KEY в .env файл'
            try:
                polza_client.chat.completions.create(
                    model=model,
                    messages=[{'role': 'user', 'content': 'test'}],
                    max_tokens=16,
                    timeout=30,
                )
                return True, None
            except Exception as e:
                return False, f'Ошибка проверки модели {model} в Polza.ai: {str(e)}'

        return False, f'Неподдерживаемый провайдер: {provider}'

    except Exception as e:
        return False, f'Непредвиденная ошибка при проверке модели: {str(e)}'


def check_providers_status() -> dict[str, tuple[bool, str]]:
    """
    Проверяет подключение ко всем провайдерам при старте.

    Returns:
        Словарь {провайдер: (доступен, сообщение)}.
    """
    result: dict[str, tuple[bool, str]] = {}

    # Ollama
    try:
        models = ollama.list()
        model_names = [m.model for m in models.get('models', [])]
        result['ollama'] = (True, f'Модели: {", ".join(model_names)}' if model_names else 'Нет моделей')
    except Exception as e:
        result['ollama'] = (False, str(e))

    # Polza.ai
    if POLZA_API_KEY:
        result['polza'] = (True, 'API ключ найден')
    else:
        result['polza'] = (False, 'POLZA_API_KEY не задан')

    return result


# =============================================================================
# ОСНОВНЫЕ ФУНКЦИИ
# =============================================================================


def generate_summary(
    article_data: dict,
    model: str = DEFAULT_MODEL,
    provider: str = DEFAULT_PROVIDER,
) -> str:
    """
    Генерирует конспект через выбранную модель.

    Args:
        article_data: Словарь с данными статьи/репозитория.
        model: Название модели.
        provider: Провайдер ('polza', 'ollama').

    Returns:
        Текст конспекта или сообщение об ошибке (начинается с '❌').
    """
    start_time = time.perf_counter()

    try:
        system_prompt, user_prompt = create_prompt(article_data)
        result = _generate(system_prompt, user_prompt, model, provider)

        elapsed = time.perf_counter() - start_time
        if not result.startswith('❌'):
            logger.info('generate_summary completed: model=%s, provider=%s, source=%s, length=%d, time=%.2fs',
                        model, provider, article_data.get('source'), len(result), elapsed)
        else:
            logger.warning('generate_summary failed: model=%s, provider=%s, error=%s, time=%.2fs',
                           model, provider, result[:100], elapsed)

        return result

    except ValueError as e:
        elapsed = time.perf_counter() - start_time
        return f'❌ Ошибка: {str(e)}'

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        return f'❌ Ошибка при генерации конспекта: {str(e)}'


def generate_idea_md(
    idea_name: str,
    idea_description: str,
    model: str = DEFAULT_MD_MODEL,
    provider: str = DEFAULT_MD_PROVIDER,
) -> str:
    """Генерирует .md-описание идеи на основе названия и описания."""
    start_time = time.perf_counter()
    user_prompt = IDEA_MD_USER_PROMPT_TEMPLATE.format(
        idea_name=idea_name,
        idea_description=idea_description or '(нет описания)',
    )
    result = _generate(IDEA_MD_SYSTEM_PROMPT, user_prompt, model, provider)
    elapsed = time.perf_counter() - start_time
    logger.info(
        'generate_idea_md completed: model=%s, provider=%s, idea=%s, length=%d, time=%.2fs',
        model, provider, idea_name[:50], len(result), elapsed,
    )
    return result


def revise_idea_md(
    current_md: str,
    feedback: str,
    model: str = DEFAULT_MD_MODEL,
    provider: str = DEFAULT_MD_PROVIDER,
) -> str:
    """Переделывает .md по замечаниям пользователя."""
    start_time = time.perf_counter()
    user_prompt = IDEA_MD_REVISE_USER_PROMPT_TEMPLATE.format(
        current_md=current_md,
        feedback=feedback,
    )
    result = _generate(IDEA_MD_REVISE_SYSTEM_PROMPT, user_prompt, model, provider)
    elapsed = time.perf_counter() - start_time
    logger.info(
        'revise_idea_md completed: model=%s, provider=%s, length=%d, time=%.2fs',
        model, provider, len(result), elapsed,
    )
    return result