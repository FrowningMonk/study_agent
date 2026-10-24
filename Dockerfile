FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Runtime dirs are mounted as volumes; create them owned by the app user
RUN useradd --create-home --uid 1000 app \
    && mkdir -p data/parsed_articles ideas_md conspect \
    && chown -R app:app /app
USER app

CMD ["python", "bot.py"]
