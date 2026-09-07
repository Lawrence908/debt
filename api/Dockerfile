FROM python:3.12-alpine

WORKDIR /app
COPY server.py /app/server.py
# Vendored from econ-core, alongside server.py because the import is flat.
COPY econcore.py /app/econcore.py

EXPOSE 8000
CMD ["python", "-u", "/app/server.py"]
