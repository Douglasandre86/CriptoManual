# --- Bibliotecas Essenciais ---
import telegram
from telegram.ext import Application, CommandHandler
import logging
import os
from dotenv import load_dotenv
import asyncio
from base64 import b64decode
import httpx
from datetime import datetime

# --- Libs da Solana ---
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

# --- Servidor Web para Keep-Alive (Necessário para Railway) ---
from flask import Flask
from threading import Thread

app = Flask('')
@app.route('/')
def home():
    return "Bot is alive!"
def run_server():
    app.run(host='0.0.0.0', port=8080)
def keep_alive():
    t = Thread(target=run_server)
    t.start()

# --- Carregamento de Variáveis de Ambiente ---
load_dotenv()

# --- Configurações de Log ---
# Configura o log para imprimir no console com formato detalhado
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- Configurações Iniciais e Validação ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY_BASE58")
JUPITER_API_URL = "https://quote-api.jup.ag/v6"

if not all([TELEGRAM_TOKEN, CHAT_ID, PRIVATE_KEY_B58]):
    logger.critical("ERRO: Variáveis de ambiente TELEGRAM_TOKEN, CHAT_ID, ou PRIVATE_KEY_BASE58 não estão definidas.")
    exit()

try:
    solana_client = Client("https://api.mainnet-beta.solana.com")
    wallet = Keypair.from_base58_string(PRIVATE_KEY_B58)
    wallet_pubkey = wallet.pubkey()
    logger.info(f"Carteira Solana carregada com sucesso: {wallet_pubkey}")
except Exception as e:
    logger.critical(f"ERRO: Falha ao inicializar a carteira Solana. Verifique sua chave privada: {e}")
    exit()

# --- Variáveis Globais de Controle ---
bot_running = False
in_position = False
entry_price = 0.0
position_high_price = 0.0 # Usado para o trailing stop
application = None
check_interval_seconds = 60 # Fixo em 60 segundos
periodic_task = None
parameters = {
    "base_token_symbol": None, "quote_token_symbol": None, "amount": None,
    "take_profit_percent": None, "trailing_stop_percent": None,
    "trade_pair_details": {}
}

# --- Funções Auxiliares ---
async def send_telegram_message(message, parse_mode='Markdown'):
    if application:
        await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=parse_mode)
    else:
        logger.warning(f"Aplicação do bot não inicializada. Não foi possível enviar: {message}")

async def fetch_dexscreener_prices(pair_address):
    try:
        url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            pair_data = data.get('pair')
            if pair_data and 'priceUsd' in pair_data:
                return {'price_usd': float(pair_data['priceUsd'])}
            logger.warning("Resposta da Dexscreener não contém dados do par ou preço USD.")
            return None
    except httpx.RequestError as e:
        logger.error(f"Erro de rede ao buscar preço na Dexscreener: {e}")
    except Exception as e:
        logger.error(f"Erro inesperado ao buscar preço na Dexscreener: {e}", exc_info=True)
    return None

async def get_jupiter_quote(from_mint, to_mint, amount_lamports):
    url = f"{JUPITER_API_URL}/quote?inputMint={from_mint}&outputMint={to_mint}&amount={amount_lamports}&slippageBps=500" # Slippage de 5%
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30); response.raise_for_status(); return response.json()
    except Exception as e:
        logger.error(f"Erro ao obter cotação da Jupiter: {e}"); return None

async def execute_swap(quote_response):
    payload = {"quoteResponse": quote_response, "userPublicKey": str(wallet_pubkey), "wrapAndUnwrapSol": True}
    try:
        async with httpx.AsyncClient() as client:
            swap_response = await client.post(f"{JUPITER_API_URL}/swap", json=payload, timeout=30)
            swap_response.raise_for_status(); swap_data = swap_response.json()
        
        raw_tx = b64decode(swap_data['swapTransaction'])
        versioned_tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = wallet.sign_transaction(versioned_tx)
        
        opts = TxOpts(skip_preflight=False, preflight_commitment="processed")
        tx_receipt = await solana_client.send_transaction(signed_tx, opts)
        
        logger.info(f"Transação de swap enviada: {tx_receipt.value}")
        await send_telegram_message(f"🚀 Transação enviada! [Ver no Solscan](https://solscan.io/tx/{tx_receipt.value})")
        return tx_receipt.value
    except Exception as e:
        logger.error(f"Erro ao executar o swap: {e}"); await send_telegram_message(f"⚠️ Falha ao executar swap: {e}"); return None

async def get_token_balance(token_mint_address):
    try:
        token_mint_pubkey = Pubkey.from_string(token_mint_address)
        assoc_token_address = get_associated_token_address(wallet_pubkey, token_mint_pubkey)
        balance_response = await solana_client.get_token_account_balance(assoc_token_address)
        return int(balance_response.value.amount)
    except Exception:
        return 0

# --- Lógica Principal de Trade ---
async def execute_buy_order():
    global in_position, entry_price, position_high_price
    if in_position: await send_telegram_message("Já existe uma posição aberta."); return
    
    details = parameters["trade_pair_details"]
    amount_to_buy = parameters["amount"]
    logger.info(f"Iniciando ordem de compra para {details['base_symbol']} com {amount_to_buy} {details['quote_symbol']}")
    
    amount_lamports = int(amount_to_buy * (10**details['quote_decimals']))
    quote = await get_jupiter_quote(details['quote_address'], details['base_address'], amount_lamports)
    
    if not quote: await send_telegram_message("⚠️ Não foi possível obter cotação da Jupiter para a compra."); return
    
    price_info = await fetch_dexscreener_prices(details['pair_address'])
    if not price_info: await send_telegram_message("⚠️ Não foi possível obter preço atual para registrar a entrada."); return
    
    current_price = price_info['price_usd']
    tx_hash = await execute_swap(quote)
    if tx_hash:
        in_position = True
        entry_price = current_price
        position_high_price = current_price
        logger.info(f"COMPRA EXECUTADA: {details['base_symbol']} @ ${current_price:.8f}. TX: {tx_hash}")
        await send_telegram_message(f"✅ *COMPRA EXECUTADA*\n\n*Token:* `{details['base_symbol']}`\n*Preço de Entrada:* `${current_price:.8f}`\n\nMonitorando Take Profit e Trailing Stop...", parse_mode='Markdown')

async def execute_sell_order(reason="Comando manual"):
    global in_position, entry_price, position_high_price
    if not in_position: await send_telegram_message("Nenhuma posição para vender."); return

    details = parameters["trade_pair_details"]
    logger.info(f"Iniciando ordem de venda para {details['base_symbol']}. Razão: {reason}")
    
    balance_lamports = await get_token_balance(details['base_address'])
    if balance_lamports == 0:
        await send_telegram_message(f"⚠️ Saldo de {details['base_symbol']} é zero. Resetando posição.");
        in_position = False; return
        
    quote = await get_jupiter_quote(details['base_address'], details['quote_address'], balance_lamports)
    if not quote: await send_telegram_message("⚠️ Não foi possível obter cotação da Jupiter para a venda."); return
    
    tx_hash = await execute_swap(quote)
    if tx_hash:
        price_info = await fetch_dexscreener_prices(details['pair_address'])
        exit_price = price_info['price_usd'] if price_info else entry_price # Fallback para entry_price
        profit_percent = ((exit_price - entry_price) / entry_price) * 100
        
        logger.info(f"VENDA EXECUTADA: {details['base_symbol']} @ ${exit_price:.8f}. Lucro/Prejuízo: {profit_percent:.2f}%. TX: {tx_hash}")
        await send_telegram_message(
            f"❌ *VENDA EXECUTADA*\n\n"
            f"*Razão:* `{reason}`\n"
            f"*Token:* `{details['base_symbol']}`\n"
            f"*Preço de Saída:* `${exit_price:.8f}`\n"
            f"*Resultado:* `{profit_percent:.2f}%`", parse_mode='Markdown'
        )
        # Reseta o estado da posição
        in_position = False
        entry_price = 0.0
        position_high_price = 0.0

# --- LÓGICA CENTRAL SIMPLIFICADA (APENAS MONITORAMENTO DE POSIÇÃO) ---
async def check_strategy():
    global in_position, entry_price, position_high_price
    
    if not bot_running or not in_position: return

    try:
        details = parameters["trade_pair_details"]
        take_profit_percent = parameters["take_profit_percent"]
        trailing_stop_percent = parameters["trailing_stop_percent"]

        # 1. Obter o preço atual
        logger.info(f"Monitorando posição em {details['base_symbol']}. Buscando preço atual...")
        price_data = await fetch_dexscreener_prices(details['pair_address'])
        
        if not price_data or not price_data.get('price_usd'):
            await send_telegram_message("⚠️ Falha ao obter o preço para monitorar a posição.")
            return
            
        real_time_price_usd = price_data['price_usd']
        logger.info(f"Preço Atual: ${real_time_price_usd:.8f}")

        # 2. Atualizar o preço máximo da posição (para o trailing stop)
        if real_time_price_usd > position_high_price:
            position_high_price = real_time_price_usd
            logger.info(f"Novo preço máximo da posição: ${position_high_price:.8f}")

        # 3. Calcular alvos
        take_profit_target_usd = entry_price * (1 + take_profit_percent / 100)
        trailing_stop_price_usd = position_high_price * (1 - trailing_stop_percent / 100)
        
        logger.info(f"Posição Aberta: Entrada ${entry_price:.6f}, Máxima ${position_high_price:.6f}, Alvo TP ${take_profit_target_usd:.6f}, Stop Móvel ${trailing_stop_price_usd:.6f}")

        # 4. Verificar se algum alvo foi atingido
        if real_time_price_usd >= take_profit_target_usd:
            await execute_sell_order(reason=f"Take Profit atingido em ${take_profit_target_usd:.6f}")
            return
            
        if real_time_price_usd <= trailing_stop_price_usd:
            await execute_sell_order(reason=f"Trailing Stop atingido em ${trailing_stop_price_usd:.6f}")
            return

    except Exception as e:
        logger.error(f"Erro em check_strategy: {e}", exc_info=True)
        await send_telegram_message(f"⚠️ Erro inesperado ao monitorar posição: {e}")

# --- Comandos do Telegram ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de trade manual para a rede Solana.\n\n'
        '**Fonte de Dados:** `Dexscreener`\n'
        '**Negociação:** `Jupiter`\n\n'
        'Use `/set` para configurar com o **ENDEREÇO DO TOKEN**:\n'
        '`/set <ENDEREÇO_DO_TOKEN> <COTAÇÃO> <VALOR> <TP_%> <TS_%>`\n\n'
        '**Exemplo (TROLL/SOL):**\n'
        '`/set 5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2 SOL 0.1 25 10`\n\n'
        '**Comandos:**\n'
        '`/run` - Inicia o monitoramento de Posição\n'
        '`/stop` - Para o bot\n'
        '`/buy` - Executa uma compra manual\n'
        '`/sell` - Executa uma venda manual (fecha a posição)', parse_mode='Markdown')

async def set_params(update, context):
    global parameters
    if bot_running: await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros."); return
    try:
        if len(context.args) != 5:
            await update.effective_message.reply_text("⚠️ *Erro: Formato incorreto.*\nUse: `/set <TOKEN> <COTAÇÃO> <VALOR> <TP_%> <TS_%>`", parse_mode='Markdown'); return
        
        base_token_contract = context.args[0]
        quote_symbol_input = context.args[1].upper()
        amount = float(context.args[2])
        take_profit_percent = float(context.args[3])
        trailing_stop_percent = float(context.args[4])
        
        await update.effective_message.reply_text("Buscando o melhor par na Dexscreener...")
        
        token_search_url = f"https://api.dexscreener.com/latest/dex/tokens/{base_token_contract}"
        async with httpx.AsyncClient() as client:
            response = await client.get(token_search_url); response.raise_for_status(); token_res = response.json()
        
        if not token_res.get('pairs'): await update.effective_message.reply_text(f"⚠️ Nenhum par encontrado para este contrato."); return
        accepted_symbols = [quote_symbol_input]
        if quote_symbol_input == 'SOL': accepted_symbols.append('WSOL')
        
        valid_pairs = [p for p in token_res['pairs'] if p.get('quoteToken', {}).get('symbol') in accepted_symbols]
        if not valid_pairs: await update.effective_message.reply_text(f"⚠️ Nenhum par com `{quote_symbol_input}` encontrado."); return
        
        trade_pair = max(valid_pairs, key=lambda p: p.get('liquidity', {}).get('usd', 0))
        base_token_symbol = trade_pair['baseToken']['symbol'].lstrip('$'); quote_token_symbol = trade_pair['quoteToken']['symbol']
        
        parameters = {
            "base_token_symbol": base_token_symbol, "quote_token_symbol": quote_token_symbol,
            "amount": amount,
            "take_profit_percent": take_profit_percent, "trailing_stop_percent": trailing_stop_percent,
            "trade_pair_details": { 
                "base_symbol": base_token_symbol, "quote_symbol": quote_token_symbol, 
                "base_address": trade_pair['baseToken']['address'], "quote_address": trade_pair['quoteToken']['address'], 
                "pair_address": trade_pair['pairAddress'], 
                "quote_decimals": 9 if quote_token_symbol in ['SOL', 'WSOL'] else 6 
            }
        }
        await update.effective_message.reply_text(
            f"✅ *Parâmetros definidos!*\n\n"
            f"🪙 *Par Encontrado:* `{base_token_symbol}/{quote_token_symbol}`\n"
            f"*Endereço do Par:* `{trade_pair['pairAddress']}`\n"
            f"💰 *Valor/Ordem:* `{amount}` {quote_symbol_input}\n"
            f"📈 *Take Profit:* `{take_profit_percent}%`\n"
            f"📉 *Trailing Stop:* `{trailing_stop_percent}%`", parse_mode='Markdown')
        logger.info(f"Parâmetros definidos: {parameters}")
    except Exception as e: 
        logger.error(f"Erro em set_params: {e}", exc_info=True)
        await update.effective_message.reply_text(f"⚠️ Erro ao configurar: {e}")

async def run_bot(update, context):
    global bot_running, periodic_task
    if not parameters["trade_pair_details"]: await update.effective_message.reply_text("Defina os parâmetros com /set primeiro."); return
    if bot_running: await update.effective_message.reply_text("O bot já está em execução."); return
    
    bot_running = True
    if periodic_task is None or periodic_task.done():
        periodic_task = asyncio.create_task(periodic_checker())
    
    logger.info("Bot iniciado com /run.")
    await update.effective_message.reply_text("✅ Bot iniciado. Use /buy para comprar e iniciar o monitoramento.")

async def stop_bot(update, context):
    global bot_running, periodic_task, in_position
    if not bot_running: await update.effective_message.reply_text("O bot já está parado."); return
    
    bot_running = False
    in_position = False # Para a verificação ao parar
    if periodic_task:
        periodic_task.cancel()
        periodic_task = None
        
    logger.info("Bot parado com /stop.")
    await update.effective_message.reply_text("⏹️ Bot parado. Todos os monitoramentos foram interrompidos.")

async def manual_buy(update, context):
    if not bot_running: await update.effective_message.reply_text("Use /run primeiro."); return
    logger.info("Comando /buy recebido.")
    await update.effective_message.reply_text("Iniciando ordem de compra...")
    await execute_buy_order()

async def manual_sell(update, context):
    if not bot_running: await update.effective_message.reply_text("Use /run primeiro."); return
    if not in_position: await update.effective_message.reply_text("Nenhuma posição aberta."); return
    logger.info("Comando /sell recebido, forçando venda...")
    await update.effective_message.reply_text("Forçando ordem de venda...")
    await execute_sell_order(reason="Venda manual via /sell")

async def periodic_checker():
    logger.info(f"Verificador periódico iniciado: intervalo de {check_interval_seconds}s.")
    while True:
        try:
            await asyncio.sleep(check_interval_seconds)
            if bot_running: 
                logger.info("Executando verificação periódica...")
                await check_strategy()
        except asyncio.CancelledError: 
            logger.info("Verificador periódico cancelado.")
            break
        except Exception as e: 
            logger.error(f"Erro no loop periódico: {e}", exc_info=True)
            await asyncio.sleep(60) # Espera um pouco mais em caso de erro

def main():
    global application
    keep_alive() # Inicia o servidor web para o Railway
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Adiciona os handlers dos comandos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("run", run_bot))
    application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("buy", manual_buy))
    application.add_handler(CommandHandler("sell", manual_sell))
    
    logger.info("Bot do Telegram iniciado, aguardando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()
