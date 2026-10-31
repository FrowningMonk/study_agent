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
import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

# Логгер модуля
logger = logging.getLogger(__name__)

# Публичный API модуля
__all__ = ['get_article', 'get_structured_habr_article']

# Константы
MAX_CONTENT_LENGTH: int = 60000
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
        with requests.Session() as session:
            session.headers.update(headers)
            response = session.get(url, timeout=TIMEOUT_SECONDS)

            challenge = _parse_cookie_challenge(response.text)
            if challenge:
                cookie_name, cookie_value, retry_url = challenge
                # Адрес взят со страницы: разрешаем относительный путь и не уходим на чужой хост
                retry_url = urljoin(response.url, retry_url)
                if urlparse(retry_url).netloc != urlparse(response.url).netloc:
                    logger.warning('Cookie challenge redirects to foreign host: url=%s, retry_url=%s',
                                   url, retry_url)
                    return None, {'error': 'Проверка на бота ведёт на посторонний сайт'}
                logger.info('Cookie challenge detected for %s, retrying with %s', url, cookie_name)
                session.cookies.set(cookie_name, cookie_value)
                response = session.get(retry_url, timeout=TIMEOUT_SECONDS)
                if _parse_cookie_challenge(response.text):
                    return None, {'error': 'Сайт не пропустил проверку на бота (JS cookie challenge)'}

            response.raise_for_status()
            # Без charset в заголовке requests берёт ISO-8859-1 — ломает cp1251 (InfoStart)
            if response.encoding and response.encoding.lower() == 'iso-8859-1':
                response.encoding = response.apparent_encoding
            return BeautifulSoup(response.text, 'html.parser'), None

    except requests.exceptions.Timeout:
        return None, {'error': 'Таймаут: сайт не ответил за 10 секунд'}
    except requests.exceptions.HTTPError as e:
        return None, {'error': f'HTTP {e.response.status_code}: {e.response.reason}'}
    except requests.exceptions.RequestException as e:
        return None, {'error': f'Ошибка сети: {str(e)}'}


# Заглушка вида: document.cookie="RCPC=<hex>; path=/";var u="<url>";...location.replace(u);
_COOKIE_CHALLENGE_RE = re.compile(
    r'document\.cookie\s*=\s*"(\w+)=([^;"]+)[^"]*".*?var\s+u\s*=\s*"([^"]+)"',
    re.S,
)
_CHALLENGE_MAX_BODY: int = 2000


def _parse_cookie_challenge(html: str) -> tuple[str, str, str] | None:
    """
    Распознаёт JS-заглушку, которая ставит куку и перезагружает страницу.

    Returns:
        (cookie_name, cookie_value, retry_url) или None, если это обычная страница.
    """
    if len(html) > _CHALLENGE_MAX_BODY:
        return None
    match = _COOKIE_CHALLENGE_RE.search(html)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


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

    # Комментарии (отдельный запрос к API; ошибка не ломает парсинг статьи)
    comments = _fetch_habr_comments(url)

    return {
        'url': url,
        'source': 'habr',
        'title': title,
        'author': author,
        'date': date,
        'content': content,
        'content_length': len(content),
        'comments': comments,
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


_HABR_ARTICLE_ID_RE = re.compile(r'/(?:articles|news|post)/(\d+)')
HABR_COMMENTS_API: str = 'https://habr.com/kek/v2/articles/{article_id}/comments/?fl=ru&hl=ru'


def _fetch_habr_comments(url: str) -> list[dict]:
    """
    Загружает комментарии статьи через JSON API Хабра (тот же, что использует сайт).

    Returns:
        Комментарии в порядке дерева обсуждения (формат — см. _make_comment).
        Пустой список, если комментариев нет или API недоступен.
    """
    match = _HABR_ARTICLE_ID_RE.search(url)
    if not match:
        return []

    try:
        response = requests.get(
            HABR_COMMENTS_API.format(article_id=match.group(1)),
            headers={'User-Agent': USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.warning('Habr comments fetch failed: url=%s, error=%s', url, e)
        return []

    raw = data.get('comments') or {}
    roots = [cid for cid, c in raw.items() if not c.get('parentId')]

    # Обход дерева в глубину: порядок как на странице
    ordered: list[tuple[str, dict]] = []
    visited: set[str] = set()
    stack = list(reversed(roots))
    while stack:
        cid = stack.pop()
        c = raw.get(cid)
        if not c or cid in visited:
            continue
        visited.add(cid)
        ordered.append((cid, c))
        stack.extend(reversed(c.get('children') or []))

    number_by_id = {cid: i for i, (cid, _) in enumerate(ordered, 1)}
    comments = []
    for cid, c in ordered:
        if c.get('status') != 'published' or c.get('isSuspended'):
            continue
        text = BeautifulSoup(c.get('message') or '', 'html.parser').get_text(' ', strip=True)
        if not text:
            continue
        comments.append(_make_comment(
            number=number_by_id[cid],
            author=(c.get('author') or {}).get('alias') or 'аноним',
            text=text,
            reply_to=number_by_id.get(c.get('parentId')),
            is_post_author=bool(c.get('isPostAuthor')),
            score=c.get('score'),
        ))
    logger.info('Habr comments fetched: url=%s, total=%d, published=%d', url, len(raw), len(comments))
    return comments


def _make_comment(
    number: int,
    author: str,
    text: str,
    reply_to: int | None = None,
    is_post_author: bool = False,
    score: int | None = None,
) -> dict:
    """Единый формат комментария для всех источников."""
    return {
        'number': number,
        'author': author,
        'text': text,
        'reply_to': reply_to,
        'is_post_author': is_post_author,
        'score': score,
    }


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

    author, author_uid = _extract_infostart_author(soup)

    # Комментарии и теги разбираем до чистки контента: она удаляет эти блоки
    comments = _extract_infostart_comments(soup, author_uid)
    tags = _extract_infostart_tags(soup)
    logger.info('InfoStart comments parsed: url=%s, comments=%d, tags=%d', url, len(comments), len(tags))

    # Текст статьи
    content = _extract_infostart_content(soup)

    return {
        'url': url,
        'source': 'infostart',
        'title': title,
        'author': author,
        'content': content,
        'content_length': len(content),
        'tags': tags,
        'comments': comments,
    }


_INFOSTART_PROFILE_RE = re.compile(r'/profile/(\d+)')


def _extract_infostart_author(soup: BeautifulSoup) -> tuple[str, str | None]:
    """
    Извлекает автора публикации из блока div.user-props.

    Returns:
        (имя вида «Иван Недомолков (nedomolkov.ivan)», id профиля или None).
    """
    props = soup.find('div', class_='user-props')
    if not props:
        return 'Не найден', None

    link = props.find('a', href=_INFOSTART_PROFILE_RE)
    uid = _INFOSTART_PROFILE_RE.search(link['href']).group(1) if link else None

    text = props.get_text(' ', strip=True)
    name = re.split(r'\s*Рейтинг:', text)[0].strip()
    name = re.sub(r'\(\s+', '(', re.sub(r'\s+\)', ')', name))
    return name or 'Не найден', uid


def _extract_infostart_tags(soup: BeautifulSoup) -> list[str]:
    """Извлекает теги публикации (p.public-tags-wrap > a.public-tag)."""
    tags = (a.get_text(strip=True) for a in soup.select('p.public-tags-wrap a.public-tag'))
    return [t for t in tags if t]


def _extract_infostart_comments(soup: BeautifulSoup, post_author_uid: str | None) -> list[dict]:
    """
    Извлекает комментарии со страницы InfoStart.

    Разметка: каждый комментарий — div.forum-message-wrap; автор лежит в соседнем
    span.message-author (data-uid — id профиля). Дерева нет: ответ помечается
    ссылкой «(N)» (a.comment-ref) в начале текста.
    """
    comments = []
    for position, wrap in enumerate(soup.select('div.forum-message-wrap'), 1):
        # Блок «лучших комментариев» дублирует сообщения из общего списка
        if wrap.find_parent('div', class_='fm-best-wrap'):
            continue
        text_elem = wrap.find('div', class_='forum-message-text')
        if not text_elem:
            continue

        block = wrap.find_parent('div', class_='mes-block-out')
        number = _infostart_comment_number(block) or position

        reply_to = None
        ref = text_elem.find('a', class_='comment-ref')
        if ref:
            ref_text = ref.get_text(strip=True)
            reply_to = int(ref_text) if ref_text.isdigit() else None
            # Убираем «(N)» из начала текста
            prev, nxt = ref.previous_sibling, ref.next_sibling
            if isinstance(prev, str) and prev.strip() == '(':
                prev.replace_with('')
            if isinstance(nxt, str):
                nxt.replace_with(re.sub(r'^\s*\)', '', nxt))
            ref.decompose()

        text = text_elem.get_text(' ', strip=True)
        if not text:
            continue

        author_elem = block.find('span', class_='message-author') if block else None
        author_link = author_elem.find('a') if author_elem else None
        uid = author_elem.get('data-uid') if author_elem else None

        comments.append(_make_comment(
            number=number,
            author=author_link.get_text(strip=True) if author_link else 'аноним',
            text=text,
            reply_to=reply_to,
            is_post_author=bool(uid and uid == post_author_uid),
        ))
    return comments


_INFOSTART_MESSAGE_ID_RE = re.compile(r'^message-target-(\d+)$')


def _infostart_comment_number(block: Tag | None) -> int | None:
    """Номер комментария на сайте из id блока (message-target-N) — на него ссылаются «(N)»."""
    match = _INFOSTART_MESSAGE_ID_RE.match(block.get('id') or '') if block else None
    return int(match.group(1)) if match else None


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

    # Удаляем служебные блоки: комментарии, теги (извлекаются отдельно),
    # кнопки голосования, галерею скриншотов, рекламу биржи фриланса
    for elem in content_elem.find_all(['div', 'p'], class_=[
        'forum-message-wrap', 'comments', 'comment',
        'public-tags-wrap', 'soc-butt-wrap', 'screen-wrap', 'freelance-block',
    ]):
        elem.decompose()

    # Вкладка файлов с дисклеймером. Класс tab-content общий (Bootstrap),
    # поэтому удаляем только блок с файлами и без текста статьи внутри
    for elem in content_elem.find_all('div', class_='tab-content'):
        if elem.find(class_='tab-file-wrap') and not elem.find(class_='detail-text'):
            elem.decompose()

    # Промо-абзац «Вступайте в нашу телеграмм-группу»
    for p in content_elem.find_all('p'):
        if p.get_text(strip=True).startswith('Вступайте в нашу телеграм'):
            p.decompose()

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
    # С токеном лимит 5000 запросов/час вместо 60
    token = os.getenv('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'

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
        stars, language, pushed_at (YYYY-MM-DD), archived, license (SPDX id),
        content, content_length, files (список найденных файлов).
        pushed_at/archived/license — None, если GitHub API недоступен.
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

    # Метаданные: сначала через API, при неудаче — из HTML
    repo_info = _fetch_github_api(f'https://api.github.com/repos/{owner}/{repo}')
    # Признаки активности есть только в API; из HTML их не достаём (None = нет данных)
    pushed_at, archived, license_id = None, None, None
    if repo_info:
        description = repo_info.get('description') or ''
        stars = str(repo_info.get('stargazers_count', 0))
        language = repo_info.get('language') or 'Не определён'
        pushed_at = (repo_info.get('pushed_at') or '')[:10] or None
        archived = repo_info.get('archived')
        license_id = (repo_info.get('license') or {}).get('spdx_id')
    else:
        description = _extract_github_description(soup)
        stars = _extract_github_stars(soup)
        language = _extract_github_language(soup)

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
        'stars': stars,
        'language': language,
        'pushed_at': pushed_at,
        'archived': archived,
        'license': license_id,
        'content': combined_content,
        'content_length': len(combined_content),
        'files': [f['path'] for f in markdown_files],
    }


def _extract_github_description(soup: BeautifulSoup) -> str:
    """Извлекает описание репозитория из meta-тега."""
    meta = soup.find('meta', attrs={'name': 'description'})
    if meta and meta.get('content'):
        # Формат: "<описание> - owner/repo"
        return meta['content'].rsplit(' - ', 1)[0].strip()
    return ''


def _extract_github_stars(soup: BeautifulSoup) -> str:
    """Извлекает количество звёзд репозитория."""
    # GitHub рендерит страницу на React, данные лежат во встроенном JSON
    match = re.search(r'"stargazerCount":(\d+)', str(soup))
    if match:
        return match.group(1)
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