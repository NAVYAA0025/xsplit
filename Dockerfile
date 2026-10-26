FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -c "from rapidocr import RapidOCR; RapidOCR()"
COPY . .

ENV PORT=10000
EXPOSE 10000
CMD gunicorn -w 1 --threads 8 --timeout 120 -b 0.0.0.0:$PORT app:app
