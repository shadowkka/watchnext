FROM python:3.12-slim
WORKDIR /app
COPY --chmod=644 app.py core.py net.py ui.py search_ui.py search_client.py torrent_meta.py subscriptions.py ./
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "app.py"]
