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
  logger.info("Iniciando servidor Flask para manter o bot ativo.")
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

if not all([TELEGRAM_TOKEN, CHAT_ID, PRIVATE_KEY_B58, RPC_URL]):
    logger.critical("ERRO FATAL: Uma ou mais variáveis de ambiente não estão definidas.")
    exit()

# --- CLIENTES PERSISTENTES ---
solana_client = None
http_client = None

try:
    logger.info("Conectando ao RPC da Solana e carregando a carteira...")
    solana_client = Client(RPC_URL)
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    logger.info(f"Carteira carregada com sucesso. Endereço público: {payer.pubkey()}")
except Exception as e:
    logger.critical(f"ERRO FATAL ao carregar a carteira Solana: {e}")
    exit()

# --- Variáveis Globais de Estado ---
bot_running = False
in_position = False
entry_price = 0.0
monitor_task = None
application = None
jupiter_api_ip = None # Variável para armazenar o IP da Jupiter

parameters = {
    "pair_address": None,
    "pair_details": None,
    "stop_loss_percent": None,
    "take_profit_percent": None,
    "priority_fee": 2000000
}

# --- FUNÇÃO DE RESOLUÇÃO DE DNS MANUAL (USANDO GOOGLE) ---
async def resolve_ip_with_doh(hostname):
    """Resolve o IP de um hostname usando a API DNS-over-HTTPS da Google."""
    logger.info(f"Resolvendo o IP para {hostname} usando DNS-over-HTTPS da Google...")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://dns.google/resolve",
                params={"name": hostname, "type": "A"},
            )
            response.raise_for_status()
            data = response.json()
            # A API da Google retorna 'Answer' mesmo em caso de falha, então verificamos o status primeiro
            if data.get("Status") == 0 and "Answer" in data:
                # Procura pelo primeiro registo do tipo 'A'
                for answer in data["Answer"]:
                    if answer.get("type") == 1: # Tipo 1 é um registo A (endereço IPv4)
                        ip_address = answer["data"]
                        logger.info(f"IP para {hostname} resolvido com sucesso: {ip_address}")
                        return ip_address
                logger.error(f"A API DoH da Google não retornou um registo 'A' para {hostname}. Resposta: {data}")
                return None
            else:
                logger.error(f"A API DoH da Google não conseguiu resolver o IP para {hostname}. Resposta: {data}")
                return None
    except Exception as e:
        logger.error(f"Falha ao resolver o IP para {hostname} via DoH da Google: {e}")
        return None

# --- Funções de Execução de Ordem ---
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps=500):
    global http_client, jupiter_api_ip
    logger.info(f"--- INICIANDO PROCESSO DE SWAP ---")
    logger.info(f"De: {amount} {input_mint_str} | Para: {output_mint_str}")
    amount_wei = int(amount * (10**input_decimals))
    
    # Se ainda não tivermos o IP da Jupiter, resolvemo-lo agora.
    if not jupiter_api_ip:
        jupiter_api_ip = await resolve_ip_with_doh("quote-api.jup.ag")
        if not jupiter_api_ip:
            await send_telegram_message("⚠️ Não foi possível resolver o endereço da API da Jupiter. A transação não pode continuar.")
            return None

    jupiter_hostname = "quote-api.jup.ag"
    headers = {"Host": jupiter_hostname}

    try:
        # ETAPA 1: Obter cotação
        logger.info(f"[SWAP 1/5] Obtendo cotação da API da Jupiter...")
        quote_url = f"https://{jupiter_api_ip}/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}"
        quote_res = await http_client.get(quote_url, headers=headers)
        quote_res.raise_for_status()
        quote_response = quote_res.json()
        logger.info("[SWAP 1/5] Cotação recebida com sucesso.")

        # ETAPA 2: Obter transação
        logger.info(f"[SWAP 2/5] Solicitando a transação de swap...")
        swap_payload = {
            "userPublicKey": str(payer.pubkey()),
            "quoteResponse": quote_response,
            "wrapAndUnwrapSol": True,
            "prioritizationFee": parameters["priority_fee"]
        }
        swap_url = f"https://{jupiter_api_ip}/v6/swap"
        swap_res = await http_client.post(swap_url, json=swap_payload, headers=headers)
        swap_res.raise_for_status()
        swap_response = swap_res.json()
        swap_tx_b64 = swap_response.get('swapTransaction')
        if not swap_tx_b64:
            logger.error(f"[ERRO SWAP] A API da Jupiter não retornou uma transação. Resposta: {swap_response}"); return None
        logger.info("[SWAP 2/5] Transação recebida com sucesso.")
        
        # ETAPA 3: Assinar a transação
        logger.info("[SWAP 3/5] Decodificando e assinando a transação...")
        raw_tx_bytes = b64decode(swap_tx_b64)
        swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
        signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
        signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])
        logger.info("[SWAP 3/5] Transação assinada.")

        # ETAPA 4: Enviar para a blockchain
        logger.info("[SWAP 4/5] Enviando a transação para a rede Solana...")
        tx_opts = TxOpts(skip_preflight=False, preflight_commitment="confirmed")
        tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value
        logger.info(f"[SWAP 4/5] Transação enviada. Assinatura: {tx_signature}")
        
        # ETAPA 5: Confirmar a transação
        logger.info(f"[SWAP 5/5] Aguardando confirmação final...")
        solana_client.confirm_transaction(tx_signature, commitment="confirmed")
        logger.info(f"[SWAP 5/5] SUCESSO! Transação confirmada: https://solscan.io/tx/{tx_signature}")
        
        return str(tx_signature)

    except Exception as e:
        logger.error(f"[ERRO SWAP] Falha crítica durante o processo de swap: {e}", exc_info=True)
        await send_telegram_message(f"⚠️ Falha na transação: {e}"); return None

async def execute_buy_order(amount, price, reason="Compra Manual"):
    global in_position, entry_price, monitor_task
    logger.info(f"Recebida ordem de compra para {amount} SOL.")
    if in_position:
        logger.warning("Compra ignorada: já existe uma posição aberta.")
        await send_telegram_message("⚠️ Já existe uma posição aberta."); return

    pair_details = parameters["pair_details"]
    logger.info(f"Iniciando processo de compra para {pair_details['base_symbol']} ao preço de {price}")
    
    tx_sig = await execute_swap(pair_details['quote_address'], pair_details['base_address'], amount, 9)

    if tx_sig:
        in_position = True
        entry_price = price
        log_message = (f"✅ COMPRA REALIZADA: {amount} SOL para {pair_details['base_symbol']}\n"
                       f"Motivo: {reason}\n"
                       f"Entrada: {price:.10f} | Alvo: {price * (1 + parameters['take_profit_percent']/100):.10f} | "
                       f"Stop: {price * (1 - parameters['stop_loss_percent']/100):.10f}\n"
                       f"https://solscan.io/tx/{tx_sig}")
        logger.info(f"Compra para {pair_details['base_symbol']} bem-sucedida. Iniciando monitoramento.")
        await send_telegram_message(log_message)

        if monitor_task is None or monitor_task.done():
            monitor_task = asyncio.create_task(monitor_position())
    else:
        logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['base_symbol']}.")
        await send_telegram_message(f"❌ FALHA NA EXECUÇÃO da compra para **{pair_details['base_symbol']}**.")

async def execute_sell_order(reason=""):
    global in_position, entry_price, monitor_task
    logger.info(f"Recebida ordem de venda. Motivo: {reason}")
    if not in_position:
        logger.warning("Venda ignorada: nenhuma posição aberta.")
        return
    
    pair_details = parameters["pair_details"]
    symbol = pair_details.get('base_symbol', 'TOKEN')
    logger.info(f"Iniciando processo de venda para {symbol}.")
    try:
        logger.info(f"A obter saldo do token {symbol}...")
        token_mint_pubkey = Pubkey.from_string(pair_details['base_address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)
        
        balance_response = solana_client.get_token_account_balance(ata_address)
        token_balance_data = balance_response.value
        amount_to_sell = token_balance_data.ui_amount
        logger.info(f"Saldo encontrado: {amount_to_sell} {symbol}.")

        if amount_to_sell is None or amount_to_sell == 0:
            logger.warning("Tentativa de venda com saldo zero. Resetando estado da posição.")
            in_position = False; entry_price = 0.0
            return

        tx_sig = await execute_swap(pair_details['base_address'], pair_details['quote_address'], amount_to_sell, token_balance_data.decimals)
        
        if tx_sig:
            log_message = (f"🛑 VENDA REALIZADA: {symbol}\n"
                           f"Motivo: {reason}\n"
                           f"https://solscan.io/tx/{tx_sig}")
            logger.info(f"Venda de {symbol} bem-sucedida. Posição fechada.")
            await send_telegram_message(log_message)
            in_position = False; entry_price = 0.0
            
            if monitor_task:
                logger.info("Cancelando tarefa de monitoramento de posição.")
                monitor_task.cancel()
                monitor_task = None
        else:
            logger.error(f"FALHA NA VENDA do token {symbol}. O bot permanecerá em posição.")
            await send_telegram_message(f"❌ FALHA NA VENDA do token {symbol}. Use /sell para tentar novamente.")

    except Exception as e:
        logger.error(f"Erro crítico durante a execução da venda de {symbol}: {e}", exc_info=True)
        await send_telegram_message(f"⚠️ Erro crítico ao vender {symbol}: {e}")

# --- Funções de Análise e Monitoramento ---
async def get_pair_details(pair_address, client=None):
    http = client if client else http_client
    if not http:
        logger.error("Erro fatal: cliente HTTP não está disponível para get_pair_details.")
        return None
        
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    
    for attempt in range(3):
        try:
            logger.info(f"A buscar detalhes do par {pair_address} na DexScreener (tentativa {attempt + 1}/3)...")
            res = await http.get(url, timeout=10.0)
            res.raise_for_status()
            data = res.json()
            pair_data = data.get('pair')
            
            if not pair_data:
                logger.warning(f"Endereço {pair_address} não encontrado na DexScreener.")
                return None
            
            logger.info(f"Detalhes de {pair_data['baseToken']['symbol']} obtidos com sucesso.")
            return {
                "pair_address": pair_data['pairAddress'], 
                "base_symbol": pair_data['baseToken']['symbol'], 
                "quote_symbol": pair_data['quoteToken']['symbol'], 
                "base_address": pair_data['baseToken']['address'], 
                "quote_address": pair_data['quoteToken']['address'],
                "price_native": float(pair_data.get('priceNative', 0))
            }
        except httpx.RequestError as e:
            logger.error(f"Erro de rede ao buscar detalhes do par (tentativa {attempt + 1}/3): {e}")
            if attempt < 2: await asyncio.sleep(1)
            else: await send_telegram_message(f"⚠️ Falha de rede ao verificar o contrato {pair_address} após 3 tentativas.")
        except Exception as e:
            logger.error(f"Erro inesperado ao processar dados do par: {e}", exc_info=True)
            return None
            
    return None

async def monitor_position():
    global in_position, entry_price
    logger.info(f"--- MONITORAMENTO DE POSIÇÃO INICIADO para {parameters['pair_details']['base_symbol']} ---")
    while in_position and bot_running:
        try:
            latest_details = await get_pair_details(parameters["pair_address"])
            if not latest_details:
                logger.warning("Não foi possível obter os detalhes do par para o monitoramento. A tentar novamente em 20s.")
                await asyncio.sleep(20)
                continue
            
            current_price = latest_details["price_native"]
            profit = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
            logger.info(f"Monitorando {parameters['pair_details']['base_symbol']}: Preço Atual = {current_price:.10f}, P/L = {profit:+.2f}%")

            take_profit_price = entry_price * (1 + parameters["take_profit_percent"] / 100)
            stop_loss_price = entry_price * (1 - parameters["stop_loss_percent"] / 100)
            
            logger.info(f"Verificando Stop Loss: {current_price:.10f} <= {stop_loss_price:.10f}?")
            if current_price <= stop_loss_price:
                logger.info("CONDIÇÃO DE STOP LOSS ATINGIDA. A iniciar venda.")
                await execute_sell_order(f"Stop Loss (-{parameters['stop_loss_percent']}%)")
                continue

            logger.info(f"Verificando Take Profit: {current_price:.10f} >= {take_profit_price:.10f}?")
            if current_price >= take_profit_price:
                logger.info("CONDIÇÃO DE TAKE PROFIT ATINGIDA. A iniciar venda.")
                await execute_sell_order(f"Take Profit (+{parameters['take_profit_percent']}%)")
                continue
            
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            logger.info("Monitoramento de posição cancelado externamente."); break
        except Exception as e:
            logger.error(f"Erro crítico no loop de monitoramento: {e}", exc_info=True); await asyncio.sleep(60)
    logger.info("--- MONITORAMENTO DE POSIÇÃO FINALIZADO ---")

# --- Comandos do Telegram ---
async def start(update, context):
    logger.info(f"Comando /start recebido do utilizador {update.effective_user.username}.")
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de operações manuais.\n\n'
        '1. Use `/set <CONTRATO> <STOP_%> <PROFIT_%>` para definir um alvo.\n'
        '2. Use `/run` para ligar o bot.\n'
        '3. Use `/buy <VALOR_SOL>` para comprar.\n'
        '4. Use `/sell` para vender a qualquer momento.\n'
        '5. Use `/status` para ver a sua posição.\n'
        '6. Use `/stop` para desligar o bot.',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    logger.info(f"Comando /set recebido com argumentos: {context.args}")
    if bot_running:
        logger.warning("Tentativa de alterar parâmetros enquanto o bot está em execução.")
        await update.effective_message.reply_text("Pare o bot com `/stop` antes de alterar os parâmetros."); return
    try:
        args = context.args
        pair_address, stop_loss, take_profit = args[0], float(args[1]), float(args[2])
        
        logger.info(f"A validar o endereço do contrato: {pair_address}")
        async with httpx.AsyncClient(timeout=10.0) as temp_client:
            pair_details = await get_pair_details(pair_address, client=temp_client)

        if not pair_details:
            logger.error("A validação do contrato falhou.")
            await update.effective_message.reply_text("⚠️ Endereço de contrato inválido ou não encontrado."); return
        
        logger.info(f"Contrato validado com sucesso. Símbolo: {pair_details['base_symbol']}")
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
        logger.warning(f"Comando /set com formato incorreto: {context.args}")
        await update.effective_message.reply_text(
            "⚠️ *Formato incorreto.*\n"
            "Use: `/set <CONTRATO> <STOP_%> <PROFIT_%>`\n"
            "Ex: `/set ADDR... 5 10`",
            parse_mode='Markdown'
        )

async def run_bot(update, context):
    global bot_running, http_client
    logger.info("Comando /run recebido.")
    if bot_running:
        logger.warning("Comando /run ignorado, bot já em execução.")
        await update.effective_message.reply_text("O bot já está em execução."); return
    
    logger.info("Iniciando o cliente de rede principal (httpx)...")
    http_client = httpx.AsyncClient(timeout=30.0)
    
    bot_running = True
    logger.info("Bot alterado para o estado 'em execução'.")
    await update.effective_message.reply_text("🚀 Bot iniciado! Pronto para receber comandos.")

async def stop_bot(update, context):
    global bot_running, monitor_task, http_client
    logger.info("Comando /stop recebido.")
    if not bot_running:
        logger.warning("Comando /stop ignorado, bot já parado.")
        await update.effective_message.reply_text("O bot já está parado."); return
    
    await update.effective_message.reply_text("Parando o bot...")
    bot_running = False
    logger.info("Bot alterado para o estado 'parado'.")
    
    if monitor_task and not monitor_task.done():
        logger.info("A cancelar a tarefa de monitoramento de posição...")
        monitor_task.cancel()
        monitor_task = None
    
    if in_position:
        logger.info("Posição aberta encontrada. A iniciar venda de emergência...")
        await execute_sell_order("Parada manual do bot")
    
    if http_client:
        logger.info("A fechar o cliente de rede principal (httpx)...")
        await http_client.aclose()
        http_client = None
    
    logger.info("Processo de paragem concluído.")
    await update.effective_message.reply_text("🛑 Bot parado. Posição (se existente) foi vendida.")

async def manual_buy(update, context):
    logger.info(f"Comando /buy recebido com argumentos: {context.args}")
    if not bot_running:
        logger.warning("Comando /buy ignorado, bot não está em execução.")
        await update.effective_message.reply_text("⚠️ O bot precisa estar em execução. Use `/run` primeiro."); return
    if in_position:
        logger.warning("Comando /buy ignorado, já existe uma posição aberta.")
        await update.effective_message.reply_text("⚠️ Já existe uma posição aberta."); return
    if not parameters.get("pair_address"):
        logger.warning("Comando /buy ignorado, nenhum alvo definido.")
        await update.effective_message.reply_text("⚠️ Nenhum alvo definido. Use `/set` primeiro."); return
        
    try:
        amount = float(context.args[0])
        if amount <= 0:
            logger.warning(f"Valor de compra inválido: {amount}")
            await update.effective_message.reply_text("⚠️ O valor deve ser positivo."); return

        pair_details = parameters["pair_details"]
        logger.info(f"A obter preço atual para a compra manual de {pair_details['base_symbol']}...")
        latest_details = await get_pair_details(pair_details['pair_address'])
        if not latest_details:
            logger.error("Não foi possível obter o preço atual para a compra.")
            await update.effective_message.reply_text("⚠️ Não foi possível obter o preço atual do alvo."); return
        
        current_price = latest_details['price_native']
        logger.info(f"Preço atual obtido: {current_price}. A iniciar a execução da compra.")
        
        await update.effective_message.reply_text(f"Iniciando compra de {amount} SOL em {pair_details['base_symbol']}...")
        await execute_buy_order(amount, current_price)

    except (IndexError, ValueError):
        logger.warning(f"Comando /buy com formato incorreto: {context.args}")
        await update.effective_message.reply_text("⚠️ *Formato incorreto.*\nUse: `/buy <VALOR>`\nEx: `/buy 0.1`", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Erro inesperado no comando /buy: {e}", exc_info=True); await update.effective_message.reply_text(f"⚠️ Erro ao executar compra: {e}")

async def manual_sell(update, context):
    logger.info("Comando /sell recebido.")
    if not in_position:
        logger.warning("Comando /sell ignorado, nenhuma posição aberta.")
        await update.effective_message.reply_text("⚠️ Nenhuma posição aberta para vender."); return
    await update.effective_message.reply_text("Forçando venda manual da posição atual...")
    await execute_sell_order(reason="Venda Manual Forçada")

async def status(update, context):
    logger.info("Comando /status recebido.")
    if not bot_running:
        await update.effective_message.reply_text("O bot está parado."); return

    if in_position:
        pair_details = parameters["pair_details"]
        symbol = pair_details['base_symbol']
        
        logger.info(f"A obter preço atual para o status de {symbol}...")
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
    logger.info("--- INICIANDO O BOT ---")
    keep_alive()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    logger.info("Configurando os handlers dos comandos do Telegram...")
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("run", run_bot))
    application.add_handler(CommandHandler("stop", stop_bot))
    application.add_handler(CommandHandler("buy", manual_buy))
    application.add_handler(CommandHandler("sell", manual_sell))
    application.add_handler(CommandHandler("status", status))
    
    logger.info("Bot do Telegram pronto. A iniciar o polling...")
    application.run_polling()

if __name__ == '__main__':
    main()

