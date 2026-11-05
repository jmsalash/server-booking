FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
# CMD is overridden per service in docker-compose.yml
CMD ["uvicorn", "app.web.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
