FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --uid 10001 --create-home wardrobe && mkdir /data && chown wardrobe:wardrobe /data
COPY core.py bot.py ./
USER wardrobe
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
CMD ["python", "bot.py"]
