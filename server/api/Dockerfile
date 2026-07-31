# Образ API. Python берётся из образа, а не из системы: на сервере Ubuntu 20.04
# со штатным Python 3.8, под который современный FastAPI уже не собирается.
FROM python:3.12-slim

WORKDIR /srv

# Зависимости отдельным слоем: правка кода не тянет за собой переустановку
# пакетов, а сборка на одноядерной машине идёт заметно быстрее.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# От root в контейнере работать незачем: наружу торчит только HTTP.
RUN useradd --system --uid 1001 retailcore
USER retailcore

EXPOSE 8000

# Один воркер: процессор на сервере один, и второй воркер только добавил бы
# переключений контекста. Нагрузка здесь — десятки запросов в минуту.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
