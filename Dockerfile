# Usar a imagem base COMPLETA do Python para máxima compatibilidade de rede
FROM python:3.12

# ------------------- CORREÇÃO DE DNS -------------------
# Força o container a usar o DNS público do Google para evitar problemas de resolução de hostname
RUN echo "nameserver 8.8.8.8" > /etc/resolv.conf
# -----------------------------------------------------

# Definir o diretório de trabalho dentro do contentor
WORKDIR /app

# Instalar as ferramentas de compilação e DIAGNÓSTICO de rede
RUN apt-get update && apt-get install -y build-essential curl pkg-config libssl-dev dnsutils iputils-ping

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

# Comando para executar os diagnósticos de rede e depois a aplicação
CMD sh -c "echo '--- INICIANDO DIAGNÓSTICO DE REDE ---' && \
           echo '--- 1. VERIFICANDO CONFIG DE DNS (/etc/resolv.conf) ---' && \
           cat /etc/resolv.conf && \
           echo '--- 2. TESTANDO RESOLUÇÃO DNS DA JUPITER (dig) ---' && \
           dig quote-api.jup.ag && \
           echo '--- 3. TESTANDO ACESSO DIRETO À JUPITER (curl) ---' && \
           curl -Iv 'https://quote-api.jup.ag/v6/quote?inputMint=So11111111111111111111111111111111111111112&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=100000' && \
           echo '--- DIAGNÓSTICOS CONCLUÍDOS, INICIANDO BOT... ---' && \
           python operations/main.py"

