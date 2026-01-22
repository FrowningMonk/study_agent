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

import json
import os

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
    'read_json_file',
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
    try:
        source = article_data.get('source', 'unknown')
        provider = _get_provider(model)

        print(f'🧠 Генерация конспекта для {source}')
        print(f'   Модель: {model} ({provider})')

        # Получаем промпты для нужного источника
        system_prompt, user_prompt = create_prompt(article_data)

        # Выбираем провайдера и генерируем
        if provider == 'ollama':
            summary = _generate_with_ollama(system_prompt, user_prompt, model)
        else:
            summary = _generate_with_openai(system_prompt, user_prompt, model)

        print('✅ Конспект успешно сгенерирован!')
        return summary

    except ValueError as e:
        # Ошибка выбора промпта
        error_message = f'❌ Ошибка: {str(e)}'
        print(error_message)
        return error_message

    except Exception as e:
        # Ошибка API
        error_message = f'❌ Ошибка при генерации конспекта: {str(e)}'
        print(error_message)
        return error_message


def read_json_file(file_path: str) -> dict | None:
    """
    Читает JSON файл и возвращает словарь.

    Args:
        file_path: Путь к JSON файлу.

    Returns:
        Словарь с данными или None при ошибке.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f'✅ Загружен JSON: {file_path}')
        print(f'📊 Найдено ключей: {len(data)}')
        return data

    except FileNotFoundError:
        print(f'❌ Файл не найден: {file_path}')
        return None
    except json.JSONDecodeError:
        print(f'❌ Ошибка чтения JSON: {file_path}')
        return None
    except Exception as e:
        print(f'❌ Ошибка: {str(e)}')
        return None


# =============================================================================
# ИНТЕРАКТИВНЫЙ РЕЖИМ
# =============================================================================


def main() -> None:
    """Интерактивный режим для тестирования."""
    print('=' * 60)
    print('🤖 ГЕНЕРАТОР КОНСПЕКТОВ')
    print('=' * 60)

    # Запрашиваем путь к JSON
    json_path = input(
        'Введите путь к JSON (Enter = data/parsed_articles/): '
    ).strip()

    if not json_path:
        # Показываем доступные файлы
        parsed_dir = 'data/parsed_articles'
        if os.path.exists(parsed_dir):
            files = [f for f in os.listdir(parsed_dir) if f.endswith('.json')]
            if files:
                print(f'\n📂 Доступные файлы в {parsed_dir}:')
                for i, f in enumerate(files, 1):
                    print(f'   {i}. {f}')
                choice = input('Выберите номер файла: ').strip()
                if choice.isdigit() and 1 <= int(choice) <= len(files):
                    json_path = os.path.join(parsed_dir, files[int(choice) - 1])

    if not json_path:
        print('❌ Файл не выбран.')
        return

    # Загружаем данные
    article_data = read_json_file(json_path)
    if not article_data:
        return

    # Показываем информацию
    print(f'\n📰 Источник: {article_data.get("source", "unknown")}')
    print(f'   Заголовок: {article_data.get("title", "Не указан")}')
    print(f'   Длина: {article_data.get("content_length", 0)} символов')

    # Выбор модели
    print('\n📊 Выбор модели:')
    print('   1 — gemma3:12b (локальная, Ollama)')
    print('   2 — gpt-3.5-turbo (OpenAI)')
    print('   3 — gpt-4 (OpenAI)')

    model_choice = input('   Ваш выбор (Enter = 1): ').strip()

    if model_choice == '2':
        model = 'gpt-3.5-turbo'
    elif model_choice == '3':
        model = 'gpt-4'
    else:
        model = DEFAULT_MODEL

    # Генерация
    print('\n' + '=' * 60)
    summary = generate_summary(article_data, model)

    print('\n' + '=' * 60)
    print('📚 КОНСПЕКТ:')
    print('=' * 60)
    print(summary)

    print('\n✨ Готово!')


if __name__ == '__main__':
    main()