FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY profiles/ ./profiles/

ENV INFOGDL_DATA=/data
ENV INFOGDL_REF=/ref
ENV PORT=8000

EXPOSE 8000
CMD ["python", "web.py"]
