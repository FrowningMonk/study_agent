import requests
from bs4 import BeautifulSoup

def get_structured_habr_article(url):
    """
    Парсит статью с Хабра структурированно, извлекая заголовок, автора, дату, текст и наличие комментариев
    :param url: URL статьи на Хабре
    :return: словарь с полями или сообщение об ошибке
    """
    try:
        # 1. Загружаем страницу
        print(f" Загружаю: {url}")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # 2. Парсим HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 3. Извлекаем данные структурированно
        
        # Заголовок
        title_elem = soup.find('h1', class_='tm-title')
        title = title_elem.get_text(strip=True) if title_elem else "Не найден"
        
        # Автор
        author_elem = soup.find('a', class_='tm-user-info__username')
        if author_elem:
            # Ищем span внутри ссылки, либо берем текст ссылки
            author_span = author_elem.find('span')
            author = author_span.get_text(strip=True) if author_span else author_elem.get_text(strip=True)
        else:
            author = "Не найден"
        
        # Дата
        date_elem = soup.find('span', class_='tm-article-datetime-published')
        date = date_elem.get_text(strip=True) if date_elem else "Не найдена"
        
        # Текст статьи
        content_elem = soup.find('div', id='post-content-body')
        if content_elem:
            # Удаляем ненужные элементы из контента
            for element in content_elem(['script', 'style', 'aside', 'div.tm-article-comments-thread']):
                element.decompose()
            content = content_elem.get_text(separator='\n', strip=True)
            # Ограничиваем длину
            content = content[:8000] if len(content) > 8000 else content
        else:
            content = "Не найден"
        
        # 4. Формируем структурированный результат
        result = {
            'url': url,
            'title': title,
            'author': author,
            'date': date,
            'content': content,
            'content_length': len(content)
        }
        
        return result
        
    except requests.exceptions.Timeout:
        return {'error': 'Время ожидания истекло (сайт не ответил за 10 сек)'}
    except requests.exceptions.HTTPError as e:
        return {'error': f'Ошибка HTTP {e.response.status_code}: {e.response.reason}'}
    except Exception as e:
        return {'error': f'Неизвестная ошибка: {str(e)}'}

def format_article_for_llm(article_data):
    """
    Форматирует статью для отправки в языковую модель
    :param article_data: словарь с данными статьи
    :return: форматированная строка
    """
    if 'error' in article_data:
        return f"Ошибка: {article_data['error']}"
    
    formatted = f"""ЗАГОЛОВОК: {article_data['title']}
АВТОР: {article_data['author']}
ДАТА ПУБЛИКАЦИИ: {article_data['date']}
URL: {article_data['url']}

ТЕКСТ СТАТЬИ:
{article_data['content']}

(Длина текста: {article_data['content_length']} символов)"""
    
    return formatted

# Блок тестирования
if __name__ == "__main__":
    test_urls = [
        "https://habr.com/ru/articles/789164/",
        # Можно добавить другие тестовые URL для проверки
    ]
    
    for url in test_urls:
        print(f"\n{'='*60}")
        print(f"ОБРАБАТЫВАЕМ: {url}")
        print('='*60)
        
        # Получаем структурированные данные
        article_data = get_structured_habr_article(url)
        
        if 'error' in article_data:
            print(f" Ошибка: {article_data['error']}")
        else:
            # Выводим ключевые поля
            print(f" Заголовок: {article_data['title']}")
            print(f" Автор: {article_data['author']}")
            print(f" Дата: {article_data['date']}")
            print(f" Длина текста: {article_data['content_length']} символов")
            print(f"\n Превью текста (первые 300 символов):")
            print('-'*40)
            print(article_data['content'][:300] + "...")
            
            # Форматируем для LLM
            print(f"\n{'='*60}")
            print("ФОРМАТИРОВАННО ДЛЯ LLM:")
            print('='*60)
            formatted = format_article_for_llm(article_data)
            print(formatted[:500] + "..." if len(formatted) > 500 else formatted)
            
            # Сохраняем в файл
            import json
            with open("parsed_article.json", "w", encoding="utf-8") as f:
                json.dump(article_data, f, ensure_ascii=False, indent=2)
            print(f"\n Структурированные данные сохранены в 'parsed_article.json'")