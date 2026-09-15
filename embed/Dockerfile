FROM python:3.11-slim

WORKDIR /embed

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /embed/requirements.txt
# The CPU index keeps multi-gigabyte CUDA wheels out of the image on x86.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r /embed/requirements.txt

COPY server.py /embed/server.py

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
