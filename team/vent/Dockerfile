FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mine.json model.py advisor.py simulator.py setup_twins.py test_model.py ./
COPY data ./data
COPY ui ./ui
USER nobody
EXPOSE 8095
CMD ["python", "simulator.py"]
