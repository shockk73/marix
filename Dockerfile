FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py db.py handlers.py scheduler.py ./
COPY providers/ ./providers/
COPY llm/ ./llm/

CMD ["python", "-u", "bot.py"]
