"""
Модуль парсинга статей из различных источников.

Поддерживаемые источники:
    - habr.com (статьи)
    - github.com (README + другие .md файлы: ARCHITECTURE.md, CONTRIBUTING.md, docs/ и т.д.)
    - infostart.ru (статьи и публикации по 1С)

Example:
    >>> from scraper import get_article
    >>> data = get_article('https://habr.com/ru/articles/123456/')
    >>> print(data['title'])
    >>> data = get_article('https://github.com/owner/repo')
    >>> print(data['files'])  # Список найденных markdown файлов
    >>> data = get_article('https://infostart.ru/1c/articles/123456/')
    >>> print(data['title'])
"""

import logging
import re
import time

import requests
from bs4 import BeautifulSoup

# Логгер модуля
logger = logging.getLogger(__name__)

# Публичный API модуля
__all__ = ['get_article', 'get_structured_habr_article']

# Константы
MAX_CONTENT_LENGTH: int = 8000
TIMEOUT_SECONDS: int = 10
USER_AGENT: str = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'


def get_article(url: str) -> dict:
    """
    Роутер: определяет источник и вызывает соответствующий парсер.

    Args:
        url: URL статьи или репозитория.

    Returns:
        Словарь с данными статьи. Гарантированные поля:
            - url: исходный URL
            - source: источник ('habr' | 'github' | 'infostart')
            - title: заголовок
            - author: автор
            - content: основной текст
            - content_length: длина текста

        При ошибке возвращает словарь с единственным полем:
            - error: описание ошибки
    """
    start_time = time.perf_counter()
    url = url.strip()

    if 'habr.com' in url:
        result = _parse_habr(url)
    elif 'github.com' in url:
        result = _parse_github(url)
    elif 'infostart.ru' in url:
        result = _parse_infostart(url)
    else:
        result = {'error': f'Источник не поддерживается: {url}'}

    elapsed = time.perf_counter() - start_time
    if 'error' not in result:
        logger.info('get_article completed: source=%s, title=%s, time=%.2fs',
                    result.get('source'), result.get('title', 'N/A')[:50], elapsed)
    else:
        logger.warning('get_article failed: url=%s, error=%s, time=%.2fs',
                       url, result.get('error'), elapsed)

    return result


def _fetch_page(url: str) -> tuple[BeautifulSoup | None, dict | None]:
    """
    Загружает HTML-страницу с обработкой ошибок.

    Args:
        url: URL страницы для загрузки.

    Returns:
        Кортеж из двух элементов:
            - BeautifulSoup объект или None при ошибке
            - None или словарь с ключом 'error'
    """
    headers = {'User-Agent': USER_AGENT}

    try:
        response = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return BeautifulSoup(response.text, 'html.parser'), None

    except requests.exceptions.Timeout:
        return None, {'error': 'Таймаут: сайт не ответил за 10 секунд'}
    except requests.exceptions.HTTPError as e:
        return None, {'error': f'HTTP {e.response.status_code}: {e.response.reason}'}
    except requests.exceptions.RequestException as e:
        return None, {'error': f'Ошибка сети: {str(e)}'}


def _truncate_content(text: str, max_length: int = MAX_CONTENT_LENGTH) -> str:
    """
    Обрезает текст до максимальной длины.

    Args:
        text: Исходный текст.
        max_length: Максимальная длина (по умолчанию MAX_CONTENT_LENGTH).

    Returns:
        Обрезанный текст с '...' в конце или исходный текст.
    """
    if len(text) > max_length:
        return text[:max_length] + '...'
    return text


# =============================================================================
# Парсер Хабра
# =============================================================================


def _parse_habr(url: str) -> dict:
    """
    Парсит статью с Хабра.

    Извлекает: заголовок, автор, дата публикации, текст статьи.

    Args:
        url: URL статьи на Хабре.

    Returns:
        Словарь с полями: url, source, title, author, date, content, content_length.
        При ошибке — словарь с полем 'error'.
    """
    soup, error = _fetch_page(url)
    if error:
        return error

    # Заголовок
    title_elem = soup.find('h1', class_='tm-title')
    title = title_elem.get_text(strip=True) if title_elem else 'Не найден'

    # Автор
    author = _extract_habr_author(soup)

    # Дата
    date_elem = soup.find('span', class_='tm-article-datetime-published')
    date = date_elem.get_text(strip=True) if date_elem else 'Не найдена'

    # Текст статьи
    content = _extract_habr_content(soup)

    return {
        'url': url,
        'source': 'habr',
        'title': title,
        'author': author,
        'date': date,
        'content': content,
        'content_length': len(content),
    }


def _extract_habr_author(soup: BeautifulSoup) -> str:
    """Извлекает имя автора статьи с Хабра."""
    author_elem = soup.find('a', class_='tm-user-info__username')
    if author_elem:
        author_span = author_elem.find('span')
        return author_span.get_text(strip=True) if author_span else author_elem.get_text(strip=True)
    return 'Не найден'


def _extract_habr_content(soup: BeautifulSoup) -> str:
    """Извлекает и очищает текст статьи с Хабра."""
    content_elem = soup.find('div', id='post-content-body')
    if not content_elem:
        return 'Не найден'

    # Удаляем ненужные теги
    for tag in content_elem(['script', 'style', 'aside']):
        tag.decompose()

    content = content_elem.get_text(separator='\n', strip=True)
    return _truncate_content(content)


# =============================================================================
# Парсер InfoStart
# =============================================================================


def _parse_infostart(url: str) -> dict:
    """
    Парсит статью с InfoStart.ru.

    Извлекает: заголовок, текст статьи. Автор и дата часто не доступны
    в основной разметке страницы.

    Args:
        url: URL статьи на InfoStart.

    Returns:
        Словарь с полями: url, source, title, author, content, content_length.
        При ошибке — словарь с полем 'error'.
    """
    soup, error = _fetch_page(url)
    if error:
        return error

    # Заголовок
    title_elem = soup.find('h1', class_='main-title')
    title = title_elem.get_text(strip=True) if title_elem else 'Не найден'

    # Автор - пытаемся найти ссылку на профиль пользователя
    author = 'Не найден'
    author_elem = soup.find('a', href=lambda x: x and '/users/' in x)
    if author_elem:
        author = author_elem.get_text(strip=True)

    # Текст статьи
    content = _extract_infostart_content(soup)

    return {
        'url': url,
        'source': 'infostart',
        'title': title,
        'author': author,
        'content': content,
        'content_length': len(content),
    }


def _extract_infostart_content(soup: BeautifulSoup) -> str:
    """Извлекает и очищает текст статьи с InfoStart."""
    # Основной контент обычно находится в div.kurs-spoiler после заголовка
    content_elem = soup.find('div', class_='kurs-spoiler')

    if not content_elem:
        # Резервный вариант: ищем в div.public-text-wrapper
        content_elem = soup.find('div', class_='public-text-wrapper')

    if not content_elem:
        # Последний резервный вариант: берем div.content
        content_elem = soup.find('div', class_='content')

    if not content_elem:
        return 'Не найден'

    # Удаляем ненужные теги
    for tag in content_elem(['script', 'style', 'aside', 'iframe', 'nav']):
        tag.decompose()

    # Удаляем служебные блоки
    for elem in content_elem.find_all('div', class_=['forum-message-wrap', 'comments', 'comment']):
        elem.decompose()

    content = content_elem.get_text(separator='\n', strip=True)

    # Удаляем строки, состоящие только из символов > (навигация)
    lines = content.split('\n')
    cleaned_lines = [line for line in lines if line.strip() and line.strip() != '>']
    content = '\n'.join(cleaned_lines)

    # Очищаем множественные пустые строки
    content = re.sub(r'\n{3,}', '\n\n', content)

    return _truncate_content(content)


# =============================================================================
# Парсер GitHub
# =============================================================================

# Список важных markdown файлов для поиска
IMPORTANT_MD_FILES = [
    'README.md',
    'ARCHITECTURE.md',
    'CONTRIBUTING.md',
    'DEVELOPMENT.md',
    'SETUP.md',
    'INSTALL.md',
    'USAGE.md',
    'API.md',
    'CHANGELOG.md',
]


def _fetch_github_api(api_url: str) -> dict | None:
    """
    Выполняет запрос к GitHub API.

    Args:
        api_url: URL эндпоинта GitHub API.

    Returns:
        JSON ответ или None при ошибке.
    """
    headers = {
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': USER_AGENT,
    }

    try:
        response = requests.get(api_url, headers=headers, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        # 404 — нормальная ситуация (папка docs может отсутствовать)
        if e.response is not None and e.response.status_code == 404:
            logger.debug('GitHub API 404: %s', api_url)
        else:
            logger.warning('Ошибка GitHub API: %s', e)
        return None
    except Exception as e:
        logger.warning('Ошибка GitHub API: %s', e)
        return None


def _find_markdown_files(owner: str, repo: str) -> list[dict]:
    """
    Находит все важные markdown файлы в репозитории.

    Args:
        owner: Владелец репозитория.
        repo: Название репозитория.

    Returns:
        Список словарей с информацией о файлах: [{'name': ..., 'path': ..., 'url': ...}, ...]
    """
    found_files = []

    # Проверяем корневую директорию
    root_api_url = f'https://api.github.com/repos/{owner}/{repo}/contents'
    root_contents = _fetch_github_api(root_api_url)

    if root_contents and isinstance(root_contents, list):
        for item in root_contents:
            if item.get('type') == 'file' and item.get('name', '').upper() in [f.upper() for f in IMPORTANT_MD_FILES]:
                found_files.append({
                    'name': item['name'],
                    'path': item['path'],
                    'download_url': item.get('download_url'),
                })

    # Проверяем папку docs/
    docs_api_url = f'https://api.github.com/repos/{owner}/{repo}/contents/docs'
    docs_contents = _fetch_github_api(docs_api_url)

    if docs_contents and isinstance(docs_contents, list):
        for item in docs_contents:
            if item.get('type') == 'file' and item.get('name', '').endswith('.md'):
                found_files.append({
                    'name': item['name'],
                    'path': item['path'],
                    'download_url': item.get('download_url'),
                })

    return found_files


def _fetch_file_content(download_url: str) -> str:
    """
    Загружает содержимое файла по download_url.

    Args:
        download_url: URL для скачивания файла.

    Returns:
        Содержимое файла или пустая строка при ошибке.
    """
    try:
        response = requests.get(download_url, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.warning('Ошибка загрузки файла %s: %s', download_url, e)
        return ''


def _combine_markdown_content(files_data: list[dict]) -> str:
    """
    Объединяет содержимое нескольких markdown файлов.

    Args:
        files_data: Список словарей с информацией о файлах.

    Returns:
        Объединённый текст всех файлов.
    """
    combined = []

    for file_info in files_data:
        content = _fetch_file_content(file_info['download_url'])
        if content:
            # Добавляем заголовок с названием файла
            combined.append(f"\n{'=' * 60}\nФАЙЛ: {file_info['path']}\n{'=' * 60}\n")
            combined.append(content)

    return '\n'.join(combined)


def _parse_github(url: str) -> dict:
    """
    Парсит репозиторий с GitHub, собирая все важные markdown файлы.

    Извлекает: название репо, владелец, описание, содержимое README и других .md файлов,
    количество звёзд, основной язык.

    Args:
        url: URL репозитория на GitHub.

    Returns:
        Словарь с полями: url, source, title, author, description,
        stars, language, content, content_length, files (список найденных файлов).
        При ошибке — словарь с полем 'error'.
    """
    # Извлекаем owner/repo из URL
    match = re.search(r'github\.com/([^/]+)/([^/]+)', url)
    if not match:
        return {'error': 'Неверный формат URL GitHub. Ожидается: github.com/owner/repo'}

    owner, repo = match.groups()
    repo = repo.rstrip('/')

    # Формируем канонический URL репозитория
    repo_url = f'https://github.com/{owner}/{repo}'

    soup, error = _fetch_page(repo_url)
    if error:
        return error

    # Описание (короткая строка под названием)
    description = ''
    desc_elem = soup.find('p', class_='f4')
    if desc_elem:
        description = desc_elem.get_text(strip=True)

    # Ищем все важные markdown файлы через API
    markdown_files = _find_markdown_files(owner, repo)

    if markdown_files:
        # Объединяем содержимое всех найденных файлов
        combined_content = _combine_markdown_content(markdown_files)

        # Обрезаем, если слишком длинный
        combined_content = _truncate_content(combined_content, max_length=MAX_CONTENT_LENGTH * 2)
    else:
        # Если API не сработал, используем старый способ (парсинг HTML)
        logger.warning('Не удалось получить файлы через API для %s, использую парсинг HTML', repo_url)
        combined_content = _extract_github_readme(soup)
        markdown_files = [{'name': 'README.md', 'path': 'README.md'}]

    return {
        'url': repo_url,
        'source': 'github',
        'title': f'{owner}/{repo}',
        'author': owner,
        'description': description,
        'stars': _extract_github_stars(soup),
        'language': _extract_github_language(soup),
        'content': combined_content,
        'content_length': len(combined_content),
        'files': [f['path'] for f in markdown_files],
    }


def _extract_github_stars(soup: BeautifulSoup) -> str:
    """Извлекает количество звёзд репозитория."""
    star_elem = soup.find('a', href=lambda x: x and '/stargazers' in x)
    if star_elem:
        star_text = star_elem.get_text(strip=True)
        numbers = re.findall(r'[\d,.]+[kK]?', star_text)
        if numbers:
            return numbers[0]
    return '0'


def _extract_github_language(soup: BeautifulSoup) -> str:
    """Извлекает основной язык программирования."""
    lang_elem = soup.find(
        'span',
        class_='color-fg-default',
        attrs={'itemprop': 'programmingLanguage'},
    )
    if lang_elem:
        return lang_elem.get_text(strip=True)
    return 'Не определён'


def _extract_github_readme(soup: BeautifulSoup) -> str:
    """Извлекает и очищает содержимое README."""
    readme_elem = soup.find('article', class_='markdown-body')

    if not readme_elem:
        return 'README не найден'

    # Удаляем ненужные элементы
    for tag in readme_elem(['script', 'style', 'svg', 'img']):
        tag.decompose()

    # Извлекаем текст
    content = readme_elem.get_text(separator='\n', strip=True)

    # Очищаем множественные пустые строки
    content = re.sub(r'\n{3,}', '\n\n', content)

    return _truncate_content(content)