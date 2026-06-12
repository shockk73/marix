FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py booking_flow.py config.py db.py handlers.py report.py scheduler.py ./
COPY providers/ ./providers/
COPY llm/ ./llm/

CMD ["python", "-u", "bot.py"]
