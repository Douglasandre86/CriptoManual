# Usar a imagem base COMPLETA do Python para máxima compatibilidade de rede
FROM python:3.12

# Definir o diretório de trabalho dentro do contentor
WORKDIR /app

# Instalar as ferramentas de compilação e DIAGNÓSTICO de rede
RUN apt-get update && apt-get install -y build-essential curl pkg-config libssl-dev dnsutils iputils-ping

# ------------------- CORREÇÃO DE DNS (MÉTODO ENTRYPOINT) -------------------
# Cria um script que será executado na inicialização do container para configurar o DNS.
RUN echo '#!/bin/sh' > /app/entrypoint.sh && \
    echo 'echo "nameserver 8.8.8.8" > /etc/resolv.conf' >> /app/entrypoint.sh && \
    echo 'exec "$@"' >> /app/entrypoint.sh

# Torna o script de entrypoint executável
RUN chmod +x /app/entrypoint.sh
# -------------------------------------------------------------------------

# Instalar a linguagem Rust (necessária para as bibliotecas da Solana)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Copiar o ficheiro de requisitos para o contentor
COPY requirements.txt .

# Atualizar o pip e instalar as bibliotecas Python
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copiar o resto do código da aplicação para o contentor
COPY . .

# Define o nosso script como o ponto de entrada do container
ENTRYPOINT ["/app/entrypoint.sh"]

# Comando padrão para executar a aplicação (será passado para o entrypoint)
CMD ["python", "operations.py"]

