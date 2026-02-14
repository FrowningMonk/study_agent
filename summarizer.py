"""
Модуль генерации конспектов через OpenAI API, Ollama и OpenRouter.

Поддерживает разные промпты для разных источников:
    - habr.com — статьи (технические, аналитические)
    - github.com — README репозиториев
    - infostart.ru — статьи и публикации по 1С

Поддерживаемые провайдеры:
    - ollama — локальные модели (любая модель из Ollama)
    - openai — облачные модели OpenAI (любая модель)
    - openrouter — OpenRouter API (любая модель)

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
load_dotenv()

# Инициализируем клиент OpenAI
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Инициализируем клиент OpenRouter (OpenAI-совместимый API)
openrouter_client = OpenAI(
    api_key=os.getenv('OPENROUTER_API_KEY'),
    base_url='https://openrouter.ai/api/v1',
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
SUPPORTED_PROVIDERS: list[str] = ['ollama', 'openai', 'openrouter']

# Модель и провайдер по умолчанию (для конспектов)
DEFAULT_PROVIDER: str = 'ollama'
DEFAULT_MODEL: str = 'gemma3:12b'

# Модель и провайдер по умолчанию (для генерации .md)
DEFAULT_MD_PROVIDER: str = 'openai'
DEFAULT_MD_MODEL: str = 'gpt-4'


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
"""

HABR_USER_PROMPT_TEMPLATE: str = """Перескажи суть статьи в 2-3 предложениях: кто автор, что сделал, каков результат. Потом одно предложение — главный вывод.

ЗАГОЛОВОК: {title}
АВТОР: {author}
ДАТА: {date}

ТЕКСТ СТАТЬИ:
{content}
"""


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
"""

INFOSTART_USER_PROMPT_TEMPLATE: str = """Перескажи суть статьи по 1С в 2-3 предложениях: какая задача решается, какой подход использован, какой результат. Потом одно предложение — главный практический вывод.

ЗАГОЛОВОК: {title}
АВТОР: {author}

ТЕКСТ СТАТЬИ:
{content}
"""


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
            'num_predict': 1000,  # аналог max_tokens
        },
    )

    logger.info('Ответ от Ollama получен: model=%s, response_length=%d', model, len(response['message']['content']))
    return response['message']['content']


# =============================================================================
# ГЕНЕРАЦИЯ ЧЕРЕЗ OPENAI
# =============================================================================


def _generate_with_openai(
    system_prompt: str,
    user_prompt: str,
    model: str,
) -> str:
    """
    Генерирует текст через OpenAI API.

    Args:
        system_prompt: Системный промпт.
        user_prompt: Пользовательский промпт.
        model: Название модели OpenAI.

    Returns:
        Сгенерированный текст.

    Raises:
        Exception: При ошибке API.
    """
    response = client.chat.completions.create(
        model=model,
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        temperature=0.3,
        max_tokens=1000,
        timeout=30,
    )

    return response.choices[0].message.content


# =============================================================================
# ГЕНЕРАЦИЯ ЧЕРЕЗ OPENROUTER
# =============================================================================


def _generate_with_openrouter(
    system_prompt: str,
    user_prompt: str,
    model: str,
) -> str:
    """
    Генерирует текст через OpenRouter API (OpenAI-совместимый).

    Args:
        system_prompt: Системный промпт.
        user_prompt: Пользовательский промпт.
        model: Название модели в OpenRouter.

    Returns:
        Сгенерированный текст.

    Raises:
        Exception: При ошибке API.
    """
    response = openrouter_client.chat.completions.create(
        model=model,
        messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        temperature=0.3,
        max_tokens=1000,
        timeout=30,
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
        provider: Провайдер ('ollama', 'openai', 'openrouter').

    Returns:
        Сгенерированный текст.

    Raises:
        ValueError: Если провайдер не поддерживается.
    """
    if provider == 'ollama':
        return _generate_with_ollama(system_prompt, user_prompt, model)
    elif provider == 'openai':
        return _generate_with_openai(system_prompt, user_prompt, model)
    elif provider == 'openrouter':
        return _generate_with_openrouter(system_prompt, user_prompt, model)
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
        provider: Провайдер ('ollama', 'openai', 'openrouter').

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

        elif provider == 'openai':
            if not os.getenv('OPENAI_API_KEY'):
                return False, 'API ключ OpenAI не найден. Добавьте OPENAI_API_KEY в .env файл'
            try:
                client.chat.completions.create(
                    model=model,
                    messages=[{'role': 'user', 'content': 'test'}],
                    max_tokens=1,
                    timeout=10,
                )
                return True, None
            except Exception as e:
                return False, f'Ошибка проверки модели {model} в OpenAI: {str(e)}'

        elif provider == 'openrouter':
            if not os.getenv('OPENROUTER_API_KEY'):
                return False, 'API ключ OpenRouter не найден. Добавьте OPENROUTER_API_KEY в .env файл'
            try:
                openrouter_client.chat.completions.create(
                    model=model,
                    messages=[{'role': 'user', 'content': 'test'}],
                    max_tokens=1,
                    timeout=10,
                )
                return True, None
            except Exception as e:
                return False, f'Ошибка проверки модели {model} в OpenRouter: {str(e)}'

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

    # OpenAI
    if os.getenv('OPENAI_API_KEY'):
        result['openai'] = (True, 'API ключ найден')
    else:
        result['openai'] = (False, 'OPENAI_API_KEY не задан')

    # OpenRouter
    if os.getenv('OPENROUTER_API_KEY'):
        result['openrouter'] = (True, 'API ключ найден')
    else:
        result['openrouter'] = (False, 'OPENROUTER_API_KEY не задан')

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
        provider: Провайдер ('ollama', 'openai', 'openrouter').

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