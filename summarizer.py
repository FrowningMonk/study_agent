"""
Модуль генерации конспектов через OpenAI API и Ollama.

Поддерживает разные промпты для разных источников:
    - habr.com — статьи (технические, аналитические)
    - github.com — README репозиториев
    - infostart.ru — статьи и публикации по 1С

Поддерживаемые провайдеры:
    - ollama — локальные модели (gemma3:12b и др.)
    - openai — облачные модели (gpt-3.5-turbo, gpt-4)

Example:
    >>> from summarizer import generate_summary
    >>> summary = generate_summary(article_data, model='gemma3:12b')
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

# Публичный API модуля
__all__ = [
    'generate_summary',
    'check_model_availability',
    'AVAILABLE_MODELS',
    'DEFAULT_MODEL',
]


# =============================================================================
# КОНФИГУРАЦИЯ МОДЕЛЕЙ
# =============================================================================

# Доступные модели: название → провайдер
AVAILABLE_MODELS: dict[str, str] = {
    'gemma3:12b': 'ollama',
    'gpt-3.5-turbo': 'openai',
    'gpt-4': 'openai',
}

# Модель по умолчанию (локальная)
DEFAULT_MODEL: str = 'gemma3:12b'


def _get_provider(model: str) -> str:
    """
    Определяет провайдера по названию модели.

    Args:
        model: Название модели.

    Returns:
        Провайдер ('ollama' или 'openai').
        Если модель неизвестна, возвращает 'ollama'.
    """
    return AVAILABLE_MODELS.get(model, 'ollama')


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
# ПРОВЕРКА ДОСТУПНОСТИ МОДЕЛЕЙ
# =============================================================================


def check_model_availability(model: str) -> tuple[bool, str | None]:
    """
    Проверяет доступность модели.

    Args:
        model: Название модели.

    Returns:
        Кортеж (доступна ли модель, сообщение об ошибке или None).
    """
    provider = _get_provider(model)

    try:
        if provider == 'ollama':
            # Проверяем доступность Ollama
            try:
                # Пытаемся получить список моделей
                models = ollama.list()
                # Проверяем, есть ли нужная модель
                available_models = [m.model for m in models.get('models', [])]
                if model not in available_models:
                    return False, f'Модель {model} не найдена в Ollama. Доступные модели: {", ".join(available_models) if available_models else "нет"}'
                return True, None
            except Exception as e:
                error_msg = str(e).lower()
                if 'connection' in error_msg or 'connect' in error_msg:
                    return False, 'Ollama не запущена. Запустите команду: ollama serve'
                elif 'keyerror' in error_msg or "'name'" in error_msg:
                    return False, f'Ошибка версии библиотеки ollama. Обновите: pip install --upgrade ollama'
                return False, f'Не удалось подключиться к Ollama. Проверьте, что сервис запущен (ollama serve)'

        elif provider == 'openai':
            # Проверяем наличие API ключа
            if not os.getenv('OPENAI_API_KEY'):
                return False, 'API ключ OpenAI не найден. Добавьте OPENAI_API_KEY в .env файл'
            # Для OpenAI дополнительная проверка не требуется
            return True, None

        return True, None

    except Exception as e:
        return False, f'Непредвиденная ошибка при проверке модели: {str(e)}'


# =============================================================================
# ОСНОВНЫЕ ФУНКЦИИ
# =============================================================================


def generate_summary(article_data: dict, model: str = DEFAULT_MODEL) -> str:
    """
    Генерирует конспект через выбранную модель.

    Args:
        article_data: Словарь с данными статьи/репозитория.
        model: Название модели (по умолчанию — локальная Ollama).

    Returns:
        Текст конспекта или сообщение об ошибке (начинается с '❌').
    """
    start_time = time.perf_counter()

    try:
        system_prompt, user_prompt = create_prompt(article_data)
        provider = _get_provider(model)

        if provider == 'ollama':
            result = _generate_with_ollama(system_prompt, user_prompt, model)
        else:
            result = _generate_with_openai(system_prompt, user_prompt, model)

        elapsed = time.perf_counter() - start_time
        if not result.startswith('❌'):
            logger.info('generate_summary completed: model=%s, source=%s, length=%d, time=%.2fs',
                        model, article_data.get('source'), len(result), elapsed)
        else:
            logger.warning('generate_summary failed: model=%s, error=%s, time=%.2fs',
                           model, result[:100], elapsed)

        return result

    except ValueError as e:
        elapsed = time.perf_counter() - start_time
        return f'❌ Ошибка: {str(e)}'

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        return f'❌ Ошибка при генерации конспекта: {str(e)}'