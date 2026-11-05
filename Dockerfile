FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --system --uid 1000 --create-home booking \
    && mkdir /data && chown booking /data
USER booking
VOLUME /data
# CMD is overridden per service in docker-compose.yml
CMD ["uvicorn", "app.web.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
