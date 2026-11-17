FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
# La copia intacta de cada STL cargado se guarda en /app/workspace (móntelo como volumen si quiere conservarla).
CMD ["python", "-m", "app.main", "--host", "0.0.0.0", "--port", "8000", "--no-browser"]
