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

# --- CÓDIGO DO SERVIDOR WEB (para manter o bot online em algumas plataformas) ---
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
    print("Erro: Verifique se todas as variáveis de ambiente (.env) estão definidas.")
    exit()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

try:
    solana_client = Client(RPC_URL)
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    logger.info(f"Carteira carregada: {payer.pubkey()}")
except Exception as e:
    logger.error(f"Erro ao carregar a carteira Solana: {e}"); exit()

# --- Estado Simplificado da Posição ---
trade_state = {
    "in_position": False,
    "entry_price": 0.0,
    "asset_details": None, # Guarda os detalhes do token comprado
    "monitoring_task": None # Guarda a tarefa que monitora o preço
}

# --- Parâmetros de Trade ---
parameters = {
    "stop_loss_percent": None,
    "take_profit_percent": None,
    "slippage_bps": 2500, # Slippage fixo em 2.5% (2500 BPS), pode ser ajustado
    "priority_fee": 1500000
}

# --- FUNÇÕES NÚCLEO (MANTIDAS DO SEU BOT ORIGINAL) ---

async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals):
    """Executa a troca usando a API da Jupiter. Reutilizada do seu bot."""
    logger.info(f"Iniciando swap de {amount} de {input_mint_str} para {output_mint_str}")
    amount_wei = int(amount * (10**input_decimals))
    
    async with httpx.AsyncClient() as client:
        try:
            # 1. Obter a cotação (quote)
            quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={parameters['slippage_bps']}&maxAccounts=64"
            quote_res = await client.get(quote_url, timeout=60.0)
            quote_res.raise_for_status()
            quote_response = quote_res.json()

            # 2. Criar a transação de swap
            swap_payload = { 
                "userPublicKey": str(payer.pubkey()), 
                "quoteResponse": quote_response, 
                "wrapAndUnwrapSol": True, 
                "dynamicComputeUnitLimit": True,
                "prioritizationFee": parameters['priority_fee']
            }
            swap_url = "https://quote-api.jup.ag/v6/swap"
            swap_res = await client.post(swap_url, json=swap_payload, timeout=60.0)
            swap_res.raise_for_status()
            swap_tx_b64 = swap_res.json().get('swapTransaction')
            if not swap_tx_b64:
                logger.error(f"Erro na API da Jupiter: {swap_res.json()}"); return None
            
            # 3. Assinar e enviar a transação
            raw_tx_bytes = b64decode(swap_tx_b64)
            swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
            signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])
            tx_opts = TxOpts(skip_preflight=True)
            tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value
            
            logger.info(f"Transação enviada, aguardando confirmação: {tx_signature}")
            solana_client.confirm_transaction(tx_signature, commitment="confirmed")
            logger.info(f"Transação confirmada: https://solscan.io/tx/{tx_signature}")
            return str(tx_signature)

        except Exception as e:
            logger.error(f"Falha no swap: {e}"); await send_telegram_message(f"⚠️ Falha no swap: {e}"); return None

async def fetch_dexscreener_real_time_price(pair_address):
    """Busca o preço atual de um par na DexScreener."""
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            pair_data = res.json().get('pair')
            if pair_data:
                return float(pair_data.get('priceNative', 0)) # Usamos priceNative para pares com SOL
        return None
    except Exception as e:
        logger.error(f"Erro ao buscar preço na DexScreener: {e}"); return None

async def get_pair_details(token_address):
    """Busca os detalhes de um token (símbolo, etc) na DexScreener."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            data = res.json().get('pairs')
            # Filtra para encontrar o par com SOL
            sol_pair = next((p for p in data if p['quoteToken']['symbol'] == 'SOL'), None)
            if not sol_pair: return None

            return {
                "pair_address": sol_pair['pairAddress'], 
                "base_symbol": sol_pair['baseToken']['symbol'], 
                "base_address": sol_pair['baseToken']['address'], 
                "quote_address": "So11111111111111111111111111111111111111112" # Endereço do wSOL
            }
    except Exception as e:
        logger.error(f"Erro ao buscar detalhes do par: {e}"); return None

# --- LÓGICA DE MONITORAMENTO (NOVO) ---

async def monitor_position():
    """Loop que monitora o preço da posição aberta e aciona TP/SL."""
    logger.info(f"Iniciando monitoramento para {trade_state['asset_details']['base_symbol']}")
    
    while trade_state["in_position"]:
        try:
            current_price = await fetch_dexscreener_real_time_price(trade_state["asset_details"]["pair_address"])
            
            if current_price and trade_state["entry_price"] > 0:
                profit_percent = ((current_price - trade_state["entry_price"]) / trade_state["entry_price"]) * 100
                logger.info(f"Monitorando {trade_state['asset_details']['base_symbol']}: Preço Atual: {current_price:.8f}, P/L: {profit_percent:+.2f}%")
                
                # Checa Take Profit
                if profit_percent >= parameters["take_profit_percent"]:
                    logger.info(f"Take Profit atingido em {profit_percent:+.2f}%. Vendendo...")
                    await send_telegram_message(f"🎯 **Take Profit!**\nLucro: `+{profit_percent:.2f}%`. Executando venda.")
                    await execute_sell_order(reason=f"Take Profit ({profit_percent:+.2f}%)")
                    break # Encerra o monitoramento após a venda

                # Checa Stop Loss
                if profit_percent <= -parameters["stop_loss_percent"]:
                    logger.info(f"Stop Loss atingido em {profit_percent:+.2f}%. Vendendo...")
                    await send_telegram_message(f"🛑 **Stop Loss!**\nPrejuízo: `{profit_percent:.2f}%`. Executando venda.")
                    await execute_sell_order(reason=f"Stop Loss ({profit_percent:+.2f}%)")
                    break # Encerra o monitoramento após a venda

            await asyncio.sleep(15) # Espera 15 segundos para a próxima verificação
        except asyncio.CancelledError:
            logger.info("Monitoramento cancelado."); break
        except Exception as e:
            logger.error(f"Erro no loop de monitoramento: {e}"); await asyncio.sleep(60)

# --- FUNÇÕES DE ORDEM (SIMPLIFICADAS) ---

async def execute_buy_order(token_address, amount_sol):
    if trade_state["in_position"]:
        await send_telegram_message("⚠️ Já existe uma posição aberta. Venda a atual primeiro."); return

    await send_telegram_message("Processando sua ordem de compra...")
    
    # 1. Pega os detalhes do token
    pair_details = await get_pair_details(token_address)
    if not pair_details:
        await send_telegram_message("❌ Não foi possível encontrar um par SOL para este token."); return
        
    # 2. Pega o preço de entrada
    entry_price = await fetch_dexscreener_real_time_price(pair_details['pair_address'])
    if not entry_price:
        await send_telegram_message("❌ Não foi possível obter o preço atual do token."); return

    # 3. Executa o swap
    tx_sig = await execute_swap("So11111111111111111111111111111111111111112", pair_details['base_address'], amount_sol, 9)

    if tx_sig:
        # 4. Atualiza o estado e inicia o monitoramento
        trade_state["in_position"] = True
        trade_state["entry_price"] = entry_price
        trade_state["asset_details"] = pair_details
        trade_state["monitoring_task"] = asyncio.create_task(monitor_position())
        
        log_message = (f"✅ **COMPRA REALIZADA**\n"
                       f"Ativo: **{pair_details['base_symbol']}**\n"
                       f"Valor: `{amount_sol}` SOL\n"
                       f"Preço de Entrada: `{entry_price:.8f}`\n"
                       f"Take Profit em: `+{parameters['take_profit_percent']}%`\n"
                       f"Stop Loss em: `-{parameters['stop_loss_percent']}%`\n"
                       f"https://solscan.io/tx/{tx_sig}")
        await send_telegram_message(log_message)
    else:
        await send_telegram_message("❌ A transação de compra falhou.")

async def execute_sell_order(reason="Comando Manual"):
    if not trade_state["in_position"]:
        await send_telegram_message("⚠️ Nenhuma posição aberta para vender."); return
    
    asset = trade_state['asset_details']
    await send_telegram_message(f"Processando venda de **{asset['base_symbol']}**...")
    
    try:
        # 1. Obter o saldo do token
        token_mint_pubkey = Pubkey.from_string(asset['base_address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)
        balance_response = solana_client.get_token_account_balance(ata_address)
        
        amount_to_sell = balance_response.value.ui_amount
        if not amount_to_sell or amount_to_sell == 0:
            await send_telegram_message("⚠️ Saldo do token é zero. Resetando posição.")
        else:
            # 2. Executar o swap de volta para SOL
            tx_sig = await execute_swap(asset['base_address'], asset['quote_address'], amount_to_sell, balance_response.value.decimals)
            if tx_sig:
                final_price = await fetch_dexscreener_real_time_price(asset['pair_address'])
                profit_percent = ((final_price - trade_state["entry_price"]) / trade_state["entry_price"]) * 100 if trade_state["entry_price"] > 0 else 0
                
                log_message = (f"🛑 **VENDA REALIZADA**\n"
                               f"Ativo: **{asset['base_symbol']}**\n"
                               f"Motivo: {reason}\n"
                               f"Resultado: `{profit_percent:+.2f}%`\n"
                               f"https://solscan.io/tx/{tx_sig}")
                await send_telegram_message(log_message)

    except Exception as e:
        logger.error(f"Erro crítico ao vender: {e}")
        await send_telegram_message(f"❌ Erro crítico ao vender: {e}. A posição pode continuar aberta.")
        return # Retorna para não resetar o estado em caso de falha na venda
    
    # 3. Limpa o estado e para o monitoramento
    if trade_state["monitoring_task"]:
        trade_state["monitoring_task"].cancel()
    trade_state.update({"in_position": False, "entry_price": 0.0, "asset_details": None, "monitoring_task": None})

# --- COMANDOS DO TELEGRAM (SIMPLIFICADOS) ---

async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Sou seu bot de trade manual para Solana.\n\n'
        '**Como funciona:**\n'
        '1️⃣ Use `/set` para definir seu take profit e stop loss.\n'
        '2️⃣ Use `/buy` com o endereço do token e o valor em SOL para comprar.\n'
        '3️⃣ Eu vou monitorar o preço e vender automaticamente no seu alvo de lucro ou de perda.\n'
        '4️⃣ Use `/sell` a qualquer momento para vender manualmente.\n'
        '5️⃣ Use `/status` para ver sua posição atual.',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    try:
        stop_loss, take_profit = float(context.args[0]), float(context.args[1])
        if stop_loss <= 0 or take_profit <= 0:
            await update.effective_message.reply_text("⚠️ Stop/Profit devem ser valores positivos."); return
        
        parameters.update(stop_loss_percent=stop_loss, take_profit_percent=take_profit)
        await update.effective_message.reply_text(
            f"✅ *Parâmetros definidos!*\n"
            f"🛑 *Stop Loss:* `-{stop_loss}%`\n"
            f"🎯 *Take Profit:* `+{take_profit}%`\n\n"
            "Agora você pode usar `/buy`.",
            parse_mode='Markdown'
        )
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "⚠️ *Formato incorreto.*\nUse: `/set <STOP_LOSS> <TAKE_PROFIT>`\n"
            "Ex: `/set 10 25` (para -10% de stop e +25% de lucro)", 
            parse_mode='Markdown'
        )

async def buy_command(update, context):
    if not all([parameters['stop_loss_percent'], parameters['take_profit_percent']]):
        await update.effective_message.reply_text("⚠️ Defina os parâmetros com /set primeiro."); return
    try:
        token_address = context.args[0]
        amount_sol = float(context.args[1])
        await execute_buy_order(token_address, amount_sol)
    except (IndexError, ValueError):
        await update.effective_message.reply_text(
            "⚠️ *Formato incorreto.*\nUse: `/buy <ENDEREÇO_DO_TOKEN> <VALOR_EM_SOL>`\n"
            "Ex: `/buy 7i5KKsX2wei1hzvx...", parse_mode='Markdown')

async def sell_command(update, context):
    await execute_sell_order()

async def status_command(update, context):
    if not trade_state["in_position"]:
        await update.effective_message.reply_text("Nenhuma posição aberta no momento."); return
    
    asset = trade_state['asset_details']
    current_price = await fetch_dexscreener_real_time_price(asset['pair_address'])
    profit_percent = ((current_price - trade_state["entry_price"]) / trade_state["entry_price"]) * 100 if current_price and trade_state["entry_price"] > 0 else 0
    
    tp_price = trade_state["entry_price"] * (1 + parameters["take_profit_percent"] / 100)
    sl_price = trade_state["entry_price"] * (1 - parameters["stop_loss_percent"] / 100)

    message = (f"📊 **Status da Posição**\n"
               f"Ativo: **{asset['base_symbol']}**\n"
               f"Preço de Entrada: `{trade_state['entry_price']:.8f}`\n"
               f"Preço Atual: `{current_price:.8f}`\n"
               f"P/L Atual: `{profit_percent:+.2f}%`\n\n"
               f"🎯 Take Profit em: `{tp_price:.8f}` (`+{parameters['take_profit_percent']}%`)\n"
               f"🛑 Stop Loss em: `{sl_price:.8f}` (`-{parameters['stop_loss_percent']}%`)")
    await update.effective_message.reply_text(message, parse_mode='Markdown')

async def send_telegram_message(message):
    if application:
        try:
            await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem para o Telegram: {e}")

# --- FUNÇÃO PRINCIPAL ---
def main():
    global application
    keep_alive() # Mantém o bot vivo em algumas plataformas
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set", set_params))
    application.add_handler(CommandHandler("buy", buy_command))
    application.add_handler(CommandHandler("sell", sell_command))
    application.add_handler(CommandHandler("status", status_command))
    
    logger.info("Bot Manual iniciado. Aguardando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()
