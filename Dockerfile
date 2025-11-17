# app/Dockerfile
FROM python:3.9-slim

WORKDIR /app

# 1. Copia APENAS o requirements.txt
COPY requirements.txt .

# 2. Instala as dependências
# (Esta etapa só vai rodar de novo se o requirements.txt mudar)
RUN pip3 install -r requirements.txt

# 3. Copia todo o resto do seu código
COPY . .

# Expõe a porta correta
EXPOSE 8502

# HEALTHCHECK corrigido para a porta 8502
HEALTHCHECK CMD curl --fail http://localhost:8502/_stcore/health

# ENTRYPOINT continua o mesmo
ENTRYPOINT ["streamlit", "run", "final_submission.py", "--server.port=8502", "--server.address=0.0.0.0"]
