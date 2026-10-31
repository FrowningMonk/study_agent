# Руководство по добавлению нового источника

Пошаговая инструкция по интеграции нового источника контента в систему агента изучения.

---

## 📋 Подготовка: Анализ источника

Перед началом работы соберите следующую информацию о новом источнике:

### 1. **Базовые параметры**

- [ ] Доменное имя (например: `infostart.ru`, `medium.com`)
- [ ] Тип контента (статьи, документация, туториалы)
- [ ] Примеры URL (минимум 2-3 разных формата)
- [ ] Целевая аудитория (для создания промпта)

### 2. **HTML структура**

Изучите HTML-разметку типичной страницы:

- [ ] Селектор заголовка (`h1`, классы)
- [ ] Селектор автора (ссылка, имя класса)
- [ ] Селектор даты публикации (если есть)
- [ ] Селектор основного контента
- [ ] Селекторы элементов для удаления (реклама, комментарии, навигация)

**Инструменты для анализа:**
- Инспектор браузера (F12)
- BeautifulSoup в Python (тестовые скрипты)

### 3. **Идентификатор статьи**

Как извлечь уникальный ID из URL для генерации имени файла:

```
Примеры:
habr.com/ru/articles/123456/        → ID: 123456
github.com/owner/repo               → ID: owner_repo
infostart.ru/public/886103/         → ID: 886103
medium.com/@user/article-slug-123   → ID: article-slug-123 или user_article-slug-123
```

---

## 🔧 Шаг 1: Создание парсера в `scraper.py`

### 1.1. Добавление функции парсинга

Создайте новую секцию в [scraper.py](../scraper.py) после существующих парсеров:

```python
# =============================================================================
# Парсер [НАЗВАНИЕ ИСТОЧНИКА]
# =============================================================================


def _parse_источник(url: str) -> dict:
    """
    Парсит контент с [Название источника].

    Извлекает: заголовок, автор, [дополнительные поля], текст.

    Args:
        url: URL статьи/контента.

    Returns:
        Словарь с полями: url, source, title, author, content, content_length.
        При ошибке — словарь с полем 'error'.
    """
    print(f'📥 Загружаю [Источник]: {url}')

    soup, error = _fetch_page(url)
    if error:
        return error

    # Заголовок
    title_elem = soup.find('h1', class_='название-класса')
    title = title_elem.get_text(strip=True) if title_elem else 'Не найден'

    # Автор
    author = 'Не найден'
    author_elem = soup.find('a', class_='author-class')
    if author_elem:
        author = author_elem.get_text(strip=True)

    # Дата (опционально)
    date = 'Не указана'
    date_elem = soup.find('time')
    if date_elem:
        date = date_elem.get_text(strip=True)

    # Текст контента
    content = _extract_источник_content(soup)

    return {
        'url': url,
        'source': 'источник',  # короткое имя
        'title': title,
        'author': author,
        'date': date,  # опционально
        'content': content,
        'content_length': len(content),
    }


def _extract_источник_content(soup: BeautifulSoup) -> str:
    """Извлекает и очищает текст контента."""
    # Основной контейнер (попробуйте несколько вариантов)
    content_elem = soup.find('div', class_='main-content')

    if not content_elem:
        # Резервные варианты
        content_elem = soup.find('article')

    if not content_elem:
        return 'Не найден'

    # Удаляем ненужные элементы
    for tag in content_elem(['script', 'style', 'aside', 'iframe', 'nav']):
        tag.decompose()

    # Удаляем служебные блоки
    for elem in content_elem.find_all('div', class_=['comments', 'ads', 'sidebar']):
        elem.decompose()

    content = content_elem.get_text(separator='\n', strip=True)

    # Очистка специфичных артефактов
    # Например, удаление навигационных символов
    lines = content.split('\n')
    cleaned_lines = [line for line in lines if line.strip() and line.strip() != '>']
    content = '\n'.join(cleaned_lines)

    # Очищаем множественные пустые строки
    content = re.sub(r'\n{3,}', '\n\n', content)

    return _truncate_content(content)
```

### 1.2. Обновление роутера

Добавьте новый источник в функцию `get_article()`:

```python
def get_article(url: str) -> dict:
    """
    Роутер: определяет источник и вызывает соответствующий парсер.
    """
    url = url.strip()

    if 'habr.com' in url:
        return _parse_habr(url)
    elif 'github.com' in url:
        return _parse_github(url)
    elif 'infostart.ru' in url:
        return _parse_infostart(url)
    elif 'источник.com' in url:  # ← ДОБАВЬТЕ ЗДЕСЬ
        return _parse_источник(url)
    else:
        return {'error': f'Источник не поддерживается: {url}'}
```

### 1.3. Обновление документации модуля

В начале [scraper.py](../scraper.py):

```python
"""
Модуль парсинга статей из различных источников.

Поддерживаемые источники:
    - habr.com (статьи)
    - github.com (README + другие .md файлы)
    - infostart.ru (статьи и публикации по 1С)
    - источник.com (описание контента)  # ← ДОБАВЬТЕ

Example:
    >>> from scraper import get_article
    >>> data = get_article('https://источник.com/article/123/')  # ← ДОБАВЬТЕ
    >>> print(data['title'])
"""
```

### 1.4. Тест парсера

Сохраните страницу статьи в `tests/fixtures/` (например, `источник_article.html`) и добавьте тест в `tests/test_scraper.py` по образцу тестов Инфостарта: подмените сетевой запрос содержимым фикстуры и проверьте заголовок, автора и текст. Тесты не должны ходить в сеть.

Запуск: `pip install -r requirements-dev.txt`, затем `pytest`.

### 1.5. Комментарии (по желанию)

Если у источника есть полезные комментарии, верните их в поле `comments` — списком словарей через `_make_comment()` (номер, автор, текст, на какой комментарий ответ, автор ли статьи, рейтинг). Тогда в шаблон промпта добавьте плейсхолдер `{comments}` и заполните его через `format_comments()` — как в промптах Хабра и Инфостарта.

---

## 🧠 Шаг 2: Создание промпта в `summarizer.py`

### 2.1. Добавление промптов

Создайте новую секцию после существующих промптов:

```python
# =============================================================================
# ПРОМПТЫ ДЛЯ [НАЗВАНИЕ ИСТОЧНИКА]
# =============================================================================

ИСТОЧНИК_SYSTEM_PROMPT: str = """Ты — [роль для данного типа контента].

Твоя задача: [описание задачи с точки зрения пользователя].

Правила:
- Пиши простым текстом без форматирования
- Никаких списков, заголовков, эмодзи
- Максимум 5-7 предложений
- [Специфичные правила для данного типа контента]
- В конце — главный вывод/рекомендация
"""

ИСТОЧНИК_USER_PROMPT_TEMPLATE: str = """[Инструкция по формированию конспекта]

ЗАГОЛОВОК: {title}
АВТОР: {author}
[ДОПОЛНИТЕЛЬНЫЕ ПОЛЯ: {field}]

ТЕКСТ:
{content}
"""
```

**Примеры ролей по типам контента:**
- Технические статьи: "помощник разработчика", "фильтр статей для занятого разработчика"
- Туториалы: "наставник по [технология]"
- Документация: "аналитик технической документации"
- Блоги: "редактор контента"

### 2.2. Создание функции генерации промпта

```python
def _create_источник_prompt(article_data: dict) -> tuple[str, str]:
    """
    Создаёт промпт для контента с [Источник].

    Args:
        article_data: Словарь с данными.

    Returns:
        Кортеж (system_prompt, user_prompt).
    """
    user_prompt = ИСТОЧНИК_USER_PROMPT_TEMPLATE.format(
        title=article_data.get('title', 'Не указан'),
        author=article_data.get('author', 'Не указан'),
        # Добавьте другие поля при необходимости
        content=article_data.get('content', 'Текст отсутствует'),
    )
    return ИСТОЧНИК_SYSTEM_PROMPT, user_prompt
```

### 2.3. Обновление роутера промптов

```python
def create_prompt(article_data: dict) -> tuple[str, str]:
    """
    Роутер: выбирает промпт в зависимости от источника.
    """
    source = article_data.get('source', 'unknown')

    if source == 'habr':
        return _create_habr_prompt(article_data)
    elif source == 'github':
        return _create_github_prompt(article_data)
    elif source == 'infostart':
        return _create_infostart_prompt(article_data)
    elif source == 'источник':  # ← ДОБАВЬТЕ ЗДЕСЬ
        return _create_источник_prompt(article_data)
    else:
        raise ValueError(f'Неподдерживаемый источник: {source}')
```

### 2.4. Обновление документации модуля

В начале [summarizer.py](../summarizer.py):

```python
"""
Модуль генерации конспектов через OpenAI API и Ollama.

Поддерживает разные промпты для разных источников:
    - habr.com — статьи (технические, аналитические)
    - github.com — README репозиториев
    - infostart.ru — статьи и публикации по 1С
    - источник.com — [описание]  # ← ДОБАВЬТЕ
"""
```

---

## 🔄 Шаг 3: Адаптация пайплайна в `pipeline.py`

### 3.1. Добавление в список источников

```python
# Поддерживаемые источники
SUPPORTED_SOURCES: dict[str, str] = {
    'habr.com': 'habr',
    'github.com': 'github',
    'infostart.ru': 'infostart',
    'источник.com': 'источник',  # ← ДОБАВЬТЕ
}
```

### 3.2. Обновление генерации имен файлов

```python
def generate_filename_from_url(url: str) -> str:
    """
    Генерирует имя файла из URL.

    Examples:
        >>> generate_filename_from_url('https://источник.com/article/123/')
        'источник_123'
    """
    source = get_source_name(url)

    if source == 'habr':
        # ...
    elif source == 'github':
        # ...
    elif source == 'infostart':
        # ...
    elif source == 'источник':  # ← ДОБАВЬТЕ
        # Логика извлечения ID для вашего источника
        # Примеры:
        # 1. Числовой ID в конце: /article/123/ → 123
        parts = url.rstrip('/').split('/')
        article_id = parts[-1] if parts[-1].isdigit() else 'unknown'
        return f'{source}_{article_id}'

        # 2. Slug в URL: /article/my-article-title/ → my-article-title
        # parts = url.rstrip('/').split('/')
        # slug = parts[-1] if parts[-1] else 'unknown'
        # return f'{source}_{slug}'

        # 3. Комбинация автор/статья: /@user/article-title → user_article-title
        # parts = url.rstrip('/').split('/')
        # if len(parts) >= 2 and parts[-2].startswith('@'):
        #     user = parts[-2][1:]  # убираем @
        #     article = parts[-1]
        #     return f'{source}_{user}_{article}'

    return f'{source}_unknown'
```

### 3.3. Обновление вывода информации (опционально)

Если у источника есть специфичные поля для вывода:

```python
def format_article_info(article_data: dict) -> None:
    """Выводит информацию о распарсенной статье."""
    source = article_data.get('source', 'unknown')

    print(f'✅ Контент распарсен: {article_data.get("title", "Без названия")}')
    print(f'   Источник: {source}')
    print(f'   Автор: {article_data.get("author", "Не указан")}')

    # Дополнительные поля в зависимости от источника
    if source == 'habr':
        print(f'   Дата: {article_data.get("date", "Не указана")}')
    elif source == 'github':
        print(f'   Звёзды: {article_data.get("stars", "0")}')
        # ...
    elif source == 'источник':  # ← ДОБАВЬТЕ при необходимости
        # Выводите специфичные поля источника
        if article_data.get('views'):
            print(f'   Просмотры: {article_data.get("views")}')
        pass

    print(f'   Длина текста: {article_data.get("content_length", 0)} символов')
```

### 3.4. Обновление документации модуля

В начале [pipeline.py](../pipeline.py):

```python
"""
Единый пайплайн: URL → Парсинг → Конспект.

Поддерживаемые источники:
    - habr.com (статьи)
    - github.com (README репозиториев)
    - infostart.ru (статьи и публикации по 1С)
    - источник.com (описание)  # ← ДОБАВЬТЕ

Example:
    Командная строка:
        python pipeline.py https://источник.com/article/123/  # ← ДОБАВЬТЕ
"""
```

---

## 🤖 Шаг 4: Интеграция с ботом в `bot.py`

### 4.1. Добавление источника

```python
# Поддерживаемые источники
SUPPORTED_SOURCES: dict[str, str] = {
    'habr.com': 'habr',
    'github.com': 'github',
    'infostart.ru': 'infostart',
    'источник.com': 'источник',  # ← ДОБАВЬТЕ
}
```

### 4.2. Обновление документации модуля

В начале [bot.py](../bot.py):

```python
"""
Telegram-бот для агента изучения ИИ.

Поддерживаемые источники:
    - habr.com (статьи)
    - github.com (README репозиториев)
    - infostart.ru (статьи и публикации по 1С)
    - источник.com (описание)  # ← ДОБАВЬТЕ
"""
```

---

## 📚 Шаг 5: Документация в `docs/sources.md`

Добавьте описание нового источника в [docs/sources.md](sources.md):

```markdown
## N. Источник.com

**Тип контента:** [Описание типа контента]

**Формат URL:**
```
https://источник.com/[путь]/[ID]/
```

**Пример:**
```
https://источник.com/article/123456/
```

**Что извлекается:**
- Заголовок
- Автор
- [Дополнительные поля]
- Полный текст (до 60 000 символов)

**Особенности:**
- [Особенность 1]
- [Особенность 2]
---

### Создание промптов

1. **Адаптируйте роль под тип контента**
   - Технические статьи → "помощник разработчика"
   - Туториалы → "наставник"
   - Новости → "редактор новостей"

2. **Фокусируйтесь на ценности для пользователя**
   - Что пользователь узнает из конспекта?
   - Стоит ли читать оригинал?
   - Каков главный практический вывод?

3. **Сохраняйте единый стиль**
   - Простой текст без форматирования
   - 5-7 предложений
   - Главный вывод в конце

### Генерация имен файлов

1. **Используйте понятные схемы**
   ```python
   источник_ID              # habr_123456
   источник_автор_статья    # medium_user_article-title
   источник_категория_ID    # docs_python_tutorial-123
   ```

2. **Обрабатывайте edge cases**
   ```python
   # Если ID не найден
   article_id = parts[-1] if parts[-1].isdigit() else 'unknown'

   # Если структура URL нестандартная
   if len(parts) < 2:
       return f'{source}_unknown'
   ```

---

## 🔍 Отладка и решение проблем

### Проблема: Парсер не находит контент

**Решение:**
1. Откройте страницу в браузере
2. Нажмите F12 → Elements
3. Найдите контейнер с текстом
4. Скопируйте селектор (ПКМ → Copy → Copy selector)
5. Адаптируйте селектор для BeautifulSoup

### Проблема: ValueError при создании промпта

**Решение:**
1. Проверьте, что source совпадает с именем в роутере
2. Убедитесь, что функция `_create_источник_prompt()` определена
3. Проверьте порядок импортов

### Проблема: Некорректная генерация имени файла

**Решение:**
1. Выведите части URL: `print(url.rstrip('/').split('/'))`
2. Проверьте индексы: `parts[-1]`, `parts[-2]`
3. Добавьте обработку нестандартных случаев

---
