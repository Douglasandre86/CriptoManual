# -*- coding: utf-8 -*-
import telegram
from telegram.ext import Application, CommandHandler
import logging
import time
import os
from dotenv import load_dotenv
import asyncio
from base64 import b64decode
import httpx

# --- Libs da Solana ---
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

from flask import Flask
from threading import Thread

# --- CÓDIGO DO SERVIDOR WEB ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is alive!"
def run_server():
  app.run(host='0.0.0.0',port=8080)
def keep_alive():
    t = Thread(target=run_server)
    t.start()
# --- FIM DO CÓDIGO DO SERVIDOR ---

load_dotenv()

# --- Configurações Iniciais ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY_BASE58")
RPC_URL = os.getenv("RPC_URL")

if not all([TELEGRAM_TOKEN, CHAT_ID, PRIVATE_KEY_B58, RPC_URL]):
    print("Erro: Verifique se todas as variáveis de ambiente estão definidas.")
    exit()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

# --- CLIENTES PERSISTENTES ---
solana_client = None
http_client = None

try:
    solana_client = Client(RPC_URL)
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    logger.info(f"Carteira carregada com sucesso. Endereço público: {payer.pubkey()}")
except Exception as e:
    logger.error(f"Erro ao carregar a carteira Solana: {e}")
    exit()

# --- Variáveis Globais de Estado ---
bot_running = False
in_position = False
entry_price = 0.0
monitor_task = None
application = None

parameters = {
    "pair_address": None,
    "pair_details": None,
    "stop_loss_percent": None,
    "take_profit_percent": None,
    "priority_fee": 2000000
}

# --- Funções de Execução de Ordem ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps=500):
    global http_client
    logger.info(f"Iniciando swap de {amount} de {input_mint_str} para {output_mint_str}")
    amount_wei = int(amount * (10**input_decimals))
    
    try:
        quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}"
        quote_res = await http_client.get(quote_url)
        quote_res.raise_for_status()
        quote_response = quote_res.json()

        swap_payload = {
            "userPublicKey": str(payer.pubkey()),
            "quoteResponse": quote_response,
            "wrapAndUnwrapSol": True,
            "prioritizationFee": parameters["priority_fee"]
        }
        
        swap_url = "https://quote-api.jup.ag/v6/swap"
        swap_res = await http_client.post(swap_url, json=swap_payload)
        swap_res.raise_for_status()
        swap_response = swap_res.json()
        swap_tx_b64 = swap_response.get('swapTransaction')
        if not swap_tx_b64:
            logger.error(f"Erro na API da Jupiter: {swap_response}"); return None
        
        raw_tx_bytes = b64decode(swap_tx_b64)
        swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
        signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
        signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])

        tx_opts = TxOpts(skip_preflight=False, preflight_commitment="confirmed")
        tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value
        
        logger.info(f"Transação enviada, aguardando confirmação: {tx_signature}")
        solana_client.confirm_transaction(tx_signature, commitment="confirmed")
        logger.info(f"Transação confirmada: https://solscan.io/tx/{tx_signature}")
        
        return str(tx_signature)

    except Exception as e:
        logger.error(f"Falha na transação: {e}"); await send_telegram_message(f"⚠️ Falha na transação: {e}"); return None

async def execute_buy_order(amount, price, reason="Compra Manual"):
    global in_position, entry_price, monitor_task
    if in_position:
        await send_telegram_message("⚠️ Já existe uma posição aberta."); return

    pair_details = parameters["pair_details"]
    logger.info(f"EXECUTANDO ORDEM DE COMPRA de {amount} SOL para {pair_details['base_symbol']} ao preço de {price}")
    
    tx_sig = await execute_swap(pair_details['quote_address'], pair_details['base_address'], amount, 9)

    if tx_sig:
        in_position = True
        entry_price = price
        log_message = (f"✅ COMPRA REALIZADA: {amount} SOL para {pair_details['base_symbol']}\n"
                       f"Motivo: {reason}\n"
                       f"Entrada: {price:.10f} | Alvo: {price * (1 + parameters['take_profit_percent']/100):.10f} | "
                       f"Stop: {price * (1 - parameters['stop_loss_percent']/100):.10f}\n"
                       f"https://solscan.io/tx/{tx_sig}")
        logger.info(log_message)
        await send_telegram_message(log_message)

        # Inicia a tarefa de monitoramento da posição
        if monitor_task is None or monitor_task.done():
            monitor_task = asyncio.create_task(monitor_position())
    else:
        logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['base_symbol']}.")
        await send_telegram_message(f"❌ FALHA NA EXECUÇÃO da compra para **{pair_details['base_symbol']}**.")

async def execute_sell_order(reason=""):
    global in_position, entry_price, monitor_task
    if not in_position: return
    
    pair_details = parameters["pair_details"]
    symbol = pair_details.get('base_symbol', 'TOKEN')
    logger.info(f"EXECUTANDO ORDEM DE VENDA de {symbol}. Motivo: {reason}")
    try:
        token_mint_pubkey = Pubkey.from_string(pair_details['base_address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)
        
        balance_response = solana_client.get_token_account_balance(ata_address)
        token_balance_data = balance_response.value
        amount_to_sell = token_balance_data.ui_amount

        if amount_to_sell is None or amount_to_sell == 0:
            logger.warning("Tentativa de venda com saldo zero, resetando posição.")
            in_position = False; entry_price = 0.0
            return

        tx_sig = await execute_swap(pair_details['base_address'], pair_details['quote_address'], amount_to_sell, token_balance_data.decimals)
        
        if tx_sig:
            log_message = (f"🛑 VENDA REALIZADA: {symbol}\n"
                           f"Motivo: {reason}\n"
                           f"https://solscan.io/tx/{tx_sig}")
            logger.info(log_message)
            await send_telegram_message(log_message)
            in_position = False; entry_price = 0.0
            
            # Para a tarefa de monitoramento
            if monitor_task:
                monitor_task.cancel()
                monitor_task = None
        else:
            logger.error(f"FALHA NA VENDA do token {symbol}. O bot permanecerá em posição.")
            await send_telegram_message(f"❌ FALHA NA VENDA do token {symbol}. Use /sell para tentar novamente ou /stop para parar o bot.")

    except Exception as e:
        logger.error(f"Erro crítico ao vender {symbol}: {e}")
        await send_telegram_message(f"⚠️ Erro crítico ao vender {symbol}: {e}")

# --- Funções de Análise e Monitoramento ---
async def get_pair_details(pair_address):
    global http_client
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        res = await http_client.get(url, timeout=10.0)
        res.raise_for_status()
        pair_data = res.json().get('pair')
        if not pair_data: return None
        return {
            "pair_address": pair_data['pairAddress'], 
            "base_symbol": pair_data['baseToken']['symbol'], 
            "quote_symbol": pair_data['quoteToken']['symbol'], 
            "base_address": pair_data['baseToken']['address'], 
            "quote_address": pair_data['quoteToken']['address'],
            "price_native": float(pair_data.get('priceNative', 0))
        }
    except Exception as e:
        logger.error(f"Erro ao buscar detalhes do par: {e}")
        return None

async def monitor_position():
    global in_position, entry_price
    logger.info(f"Monitoramento de posição iniciado para {parameters['pair_details']['base_symbol']}.")
    while in_position and bot_running:
        try:
            latest_details = await get_pair_details(parameters["pair_address"])
            if not latest_details:
                await asyncio.sleep(20)
                continue
            
            current_price = latest_details["price_native"]
            profit = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
            logger.info(f"Em Posição ({parameters['pair_details']['base_symbol']}): Preço Atual = {current_price:.10f}, P/L = {profit:+.2f}%")

            take_profit_price = entry_price * (1 + parameters["take_profit_percent"] / 100)
            stop_loss_price = entry_price * (1 - parameters["stop_loss_percent"] / 100)

            if current_price >= take_profit_price:
                await execute_sell_order(f"Take Profit (+{parameters['take_profit_percent']}%)")
            elif current_price <= stop_loss_price:
                await execute_sell_order(f"Stop Loss (-{parameters['stop_loss_percent']}%)")
            
            await asyncio.sleep(15) # Verifica a cada 15 segundos
        except asyncio.CancelledError:
            logger.info("Monitoramento de posição cancelado."); break
        except Exception as e:
            logger.error(f"Erro no monitoramento de posição: {e}"); await asyncio.sleep(60)
    logger.info("Monitoramento de posição finalizado.")

# --- Comandos do Telegram ---
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de operações manuais.\n\n'
        '1. Use `/set <CONTRATO> <STOP_%> <PROFIT_%>` para definir um alvo.\n'
        '2. Use `/run` para ligar o bot.\n'
        '3. Use `/buy <VALOR_SOL>` para comprar.\n'
        '4. Use `/sell` para vender a qualquer momento.\n'
        '5. Use `/status` para ver a sua posição.\n'
        '6. Use `/stop` para desligar o bot (vende a posição se estiver aberta).',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    if bot_running:
        await update.effective_message.reply_text("Pare o bot com `/stop` antes de alterar os parâmetros."); return
    try:
        args = context.args
        pair_address, stop_loss, take_profit = args[0], float(args[1]), float(args[2])
        
        # O http_client precisa estar ativo para esta verificação
        async with httpx.AsyncClient(timeout=10.0) as client:
            global http_client
            temp_client, http_client = http_client, client
            pair_details = await get_pair_details(pair_address)
            http_client = temp_client

        if not pair_details:
            await update.effective_message.reply_text("⚠️ Endereço de contrato inválido ou não encontrado."); return

        parameters.update(
            pair_address=pair_address,
            pair_details=pair_details,
            stop_loss_percent=stop_loss,
            take_profit_percent=take_profit
        )

        await update.effective_message.reply_text(
            f"✅ *Alvo definido para {pair_details['base_symbol']}!*\n"
            f"🛑 *Stop Loss:* `-{stop_loss}%`\n"
            f"🎯 *Take Profit:* `+{take_profit}%`\n\n"
            "Use `/run` e depois `/buy <VALOR>` para começar.",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "⚠️ *Formato incorreto.*\n"
            "Use: `/set <CONTRATO> <STOP_%> <PROFIT_%>`\n"
            "Ex: `/set ADDR... 5 10`",
            parse_mode='Markdown'
        )

async def run_bot(update, context):
    global bot_running, http_client
    if bot_running:
        await update.effective_message.reply_text("O bot já está em execução."); return
    
    http_client = httpx.AsyncClient(timeout=30.0)
    bot_running = True
    logger.info("Bot iniciado em modo manual.")
    await update.effective_message.reply_text("🚀 Bot iniciado! Use `/set` para definir um alvo e `/buy` para comprar.")

async def stop_bot(update, context):
    global bot_running, monitor_task, http_client
    if not bot_running:
        await update.effective_message.reply_text("O bot já está parado."); return
    
    await update.effective_message.reply_text("Parando o bot...")
    bot_running = False
    if monitor_task:
        monitor_task.cancel()
        monitor_task = None
    
    if in_position:
        await execute_sell_order("Parada manual do bot")
    
    if http_client:
        await http_client.aclose()
        http_client = None
    
    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado. Posição (se existente) foi vendida.")

async def manual_buy(update, context):
    if not bot_running:
        await update.effective_message.reply_text("⚠️ O bot precisa estar em execução. Use `/run` primeiro."); return
    if in_position:
        await update.effective_message.reply_text("⚠️ Já existe uma posição aberta."); return
    if not parameters.get("pair_address"):
        await update.effective_message.reply_text("⚠️ Nenhum alvo definido. Use `/set` primeiro."); return
        
    try:
        amount = float(context.args[0])
        if amount <= 0:
            await update.effective_message.reply_text("⚠️ O valor deve ser positivo."); return

        pair_details = parameters["pair_details"]
        latest_details = await get_pair_details(pair_details['pair_address'])
        if not latest_details:
            await update.effective_message.reply_text("⚠️ Não foi possível obter o preço atual do alvo."); return
        
        current_price = latest_details['price_native']
        
        await update.effective_message.reply_text(f"Iniciando compra de {amount} SOL em {pair_details['base_symbol']}...")
        await execute_buy_order(amount, current_price)

    except (IndexError, ValueError):
        await update.effective_message.reply_text("⚠️ *Formato incorreto.*\nUse: `/buy <VALOR>`\nEx: `/buy 0.1`", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Erro no comando /buy: {e}"); await update.effective_message.reply_text(f"⚠️ Erro ao executar compra: {e}")

async def manual_sell(update, context):
    if not in_position:
        await update.effective_message.reply_text("⚠️ Nenhuma posição aberta para vender."); return
    await update.effective_message.reply_text("Forçando venda manual da posição atual...")
    await execute_sell_order(reason="Venda Manual Forçada")

async def status(update, context):
    if not bot_running:
        await update.effective_message.reply_text("O bot está parado."); return

    if in_position:
        pair_details = parameters["pair_details"]
        symbol = pair_details['base_symbol']
        
        latest_details = await get_pair_details(parameters["pair_address"])
        current_price = latest_details["price_native"] if latest_details else entry_price
        
        profit = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
        
        take_profit_price = entry_price * (1 + parameters["take_profit_percent"] / 100)
        stop_loss_price = entry_price * (1 - parameters["stop_loss_percent"] / 100)

        message = (f"✅ **Posição Aberta em {symbol}**\n\n"
                   f"Preço de Entrada: `{entry_price:.10f}`\n"
                   f"Preço Atual: `{current_price:.10f}`\n"
                   f"P/L Atual: `{profit:+.2f}%`\n\n"
                   f"Take Profit: `{take_profit_price:.10f}` (+{parameters['take_profit_percent']}%)\n"
                   f"Stop Loss: `{stop_loss_price:.10f}` (-{parameters['stop_loss_percent']}%)")
    else:
        message = "ℹ️ Nenhuma posição aberta. Aguardando comando de compra."
        if parameters.get("pair_address"):
            message += f"\nAlvo atual: **{parameters['pair_details']['base_symbol']}**"

    await update.effective_message.reply_text(message, parse_mode='Markdown')

async def send_telegram_message(message):
    if application:
        try:
            await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem para o Telegram: {e}")

def main():
    global application
    keep_alive()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("run", run_bot))
    application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("buy", manual_buy))
    application.add_handler(CommandHandler("sell", manual_sell))
    application.add_handler(CommandHandler("status", status))
    
    logger.info("Bot do Telegram iniciado e aguardando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()

