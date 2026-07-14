FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ ./backend/
COPY frontend/ ./frontend/
ENV DATA_DIR=/tmp/data PYTHONUNBUFFERED=1
EXPOSE 8000
# $PORT is provided by Render; server.py reads it (default 8000 locally).
CMD ["python", "backend/server.py"]
