FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=644 app.py core.py net.py ui.py search_ui.py search_client.py torrent_meta.py subscriptions.py server.py integrations.py release_search.py catalog_client.py catalog_ui.py ./
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "server.py"]
