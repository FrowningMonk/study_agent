"""
Модуль парсинга статей из различных источников.

Поддерживаемые источники:
    - habr.com (статьи)
    - github.com (README репозиториев)

Example:
    >>> from scraper import get_article
    >>> data = get_article('https://habr.com/ru/articles/123456/')
    >>> print(data['title'])
"""

import re

import requests
from bs4 import BeautifulSoup

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
            - source: источник ('habr' | 'github')
            - title: заголовок
            - author: автор
            - content: основной текст
            - content_length: длина текста
        
        При ошибке возвращает словарь с единственным полем:
            - error: описание ошибки
    """
    url = url.strip()

    if 'habr.com' in url:
        return _parse_habr(url)
    elif 'github.com' in url:
        return _parse_github(url)
    else:
        return {'error': f'Источник не поддерживается: {url}'}


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
    print(f'📥 Загружаю Хабр: {url}')

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
# Парсер GitHub
# =============================================================================


def _parse_github(url: str) -> dict:
    """
    Парсит README репозитория с GitHub.

    Извлекает: название репо, владелец, описание, содержимое README,
    количество звёзд, основной язык.

    Args:
        url: URL репозитория на GitHub.

    Returns:
        Словарь с полями: url, source, title, author, description,
        stars, language, content, content_length.
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
    print(f'📥 Загружаю GitHub: {repo_url}')

    soup, error = _fetch_page(repo_url)
    if error:
        return error

    # Описание (короткая строка под названием)
    description = ''
    desc_elem = soup.find('p', class_='f4')
    if desc_elem:
        description = desc_elem.get_text(strip=True)

    return {
        'url': repo_url,
        'source': 'github',
        'title': f'{owner}/{repo}',
        'author': owner,
        'description': description,
        'stars': _extract_github_stars(soup),
        'language': _extract_github_language(soup),
        'content': _extract_github_readme(soup),
        'content_length': len(_extract_github_readme(soup)),
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


# =============================================================================
# Точка входа для тестирования
# =============================================================================


def _run_tests() -> None:
    """Запускает тесты парсеров."""
    test_urls = [
        'https://habr.com/ru/articles/984968/',
        'https://github.com/anthropics/anthropic-cookbook',
    ]

    for url in test_urls:
        print(f'\n{"=" * 60}')
        print(f'ТЕСТ: {url}')
        print('=' * 60)

        result = get_article(url)

        if 'error' in result:
            print(f'❌ Ошибка: {result["error"]}')
        else:
            print(f'✅ Источник: {result["source"]}')
            print(f'   Заголовок: {result["title"]}')
            print(f'   Автор: {result["author"]}')
            print(f'   Длина контента: {result["content_length"]} символов')
            print(f'\n   Превью (200 символов):')
            print(f'   {result["content"][:200]}...')


if __name__ == '__main__':
    _run_tests()