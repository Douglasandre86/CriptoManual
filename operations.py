# sniper_bot.py
import os
import time
import logging
import asyncio
from datetime import datetime, timezone
from base64 import b64decode
import httpx
import pandas as pd
from collections import Counter

from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.message import to_bytes_versioned

from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address

from telegram.ext import Application, CommandHandler
import telegram

from flask import Flask
from threading import Thread

# --- CÓDIGO DO SERVIDOR WEB ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is alive!"
def run_server():
  app.run(host='0.0.0.0',port=8000)
def keep_alive():
    t = Thread(target=run_server)
    t.start()
# --- FIM DO CÓDIGO DO SERVIDOR ---

# ---------------- Configuração ----------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("sniper_bot")

# Variáveis de ambiente (Koyeb)
# --- Configurações Iniciais ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PRIVATE_KEY_B58 = os.getenv("PRIVATE_KEY_BASE58")
RPC_URL = os.getenv("RPC_URL")

# REMOVING: MIN_LIQUIDITY and MIN_VOLUME_H1 as they are for auto-discovery
# MIN_LIQUIDITY = 40000  # Mínimo de $40,000 de liquidez
# MIN_VOLUME_H1 = 100000 # Mínimo de $100,000 de volume na última hora
TRADE_INTERVAL_SECONDS = 30

if not all([RPC_URL, PRIVATE_KEY_B58, TELEGRAM_TOKEN, CHAT_ID]):
    logger.error("Erro: variáveis de ambiente RPC_URL, PRIVATE_KEY_B58, TELEGRAM_TOKEN e CHAT_ID são obrigatórias.")
    raise SystemExit(1)

try:
    CHAT_ID = int(CHAT_ID)
except Exception:
    logger.error("CHAT_ID deve ser um inteiro (ID do chat).")
    raise SystemExit(1)

# Solana client (sync) e payer (a partir do Base58 private key que você informou)
solana_client = Client(RPC_URL)
try:
    payer = Keypair.from_base58_string(PRIVATE_KEY_B58)
    logger.info(f"Carteira carregada. Pubkey: {payer.pubkey()}")
except Exception as e:
    logger.error(f"Erro ao carregar private key: {e}")
    raise

# ---------------- Estado global ----------------
application = None

bot_running = False
periodic_task = None

in_position = False
entry_price = 0.0 # Initialize entry_price

sell_fail_count = 0
buy_fail_count = 0

automation_state = {
    "current_target_pair_address": None,
    "current_target_symbol": None,
    "current_target_pair_details": None,
    # REMOVING: last_scan_timestamp as it's for auto-discovery
    # "last_scan_timestamp": 0,
    "position_opened_timestamp": 0,
    "target_selected_timestamp": 0,
    # REMOVING: penalty_box, discovered_pairs, took_profit_pairs as they are for auto-discovery
    # "penalty_box": {},
    # "discovered_pairs": {},
    # "took_profit_pairs": set(),
    # REMOVING: checking_volatility, volatility_check_start_time
    # "checking_volatility": False,
    # "volatility_check_start_time": 0,
    "is_running": False # Add this state variable
}

parameters = {
    "timeframe": "1m",
    "amount": None,                 # em SOL - This will now only be used by /buy
    "stop_loss_percent": None,      # ex: 15
    "take_profit_percent": None,    # ex: 20
    "priority_fee": 2000000,
    "target_token_address": None  # Add this parameter
}

# ---------------- Utilitários Telegram ----------------
async def send_telegram_message(message):
    """Envia mensagem para o chat configurado (usa application global)."""
    if application:
        try:
            await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
        except telegram.error.RetryAfter as e:
            logger.warning(f"Telegram flood control: aguardando {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
            try:
                await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
            except Exception as e2:
                logger.error(f"Falha ao reenviar mensagem ao Telegram: {e2}")
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem para Telegram: {e}")
    else:
        logger.info("application não inicializado; mensagem Telegram não enviada.")

# ---------------- Funções de swap / slippage (seu código) ----------------
async def execute_swap(input_mint_str, output_mint_str, amount, input_decimals, slippage_bps):
    logger.info(f"Iniciando swap de {amount} do token {input_mint_str} para {output_mint_str} com slippage de {slippage_bps} BPS e limite computacional dinâmico.")
    amount_wei = int(amount * (10**input_decimals))

    max_retries = 5
    for attempt in range(max_retries):
        async with httpx.AsyncClient() as client:
            try:
                # Atualizado para o novo endpoint da Jupiter
                quote_url = f"https://lite-api.jup.ag/swap/v1/quote?inputMint={input_mint_str}&outputMint={output_mint_str}&amount={amount_wei}&slippageBps={slippage_bps}&maxAccounts=64"
                quote_res = await client.get(quote_url, timeout=60.0)
                quote_res.raise_for_status()
                quote_response = quote_res.json()

                swap_payload = {
                    "userPublicKey": str(payer.pubkey()),
                    "quoteResponse": quote_response,
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": {
                        "priorityLevelWithMaxLamports": {
                            "maxLamports": 10000000,
                            "priorityLevel": "veryHigh"
                        }
                    }
                }

                # Atualizado para o novo endpoint da Jupiter
                swap_url = "https://lite-api.jup.ag/swap/v1/swap"
                swap_res = await client.post(swap_url, json=swap_payload, timeout=60.0)
                swap_res.raise_for_status()
                swap_response = swap_res.json()
                swap_tx_b64 = swap_response.get('swapTransaction')
                if not swap_tx_b64:
                    logger.error(f"Erro na API da Jupiter: {swap_response}"); return None

                raw_tx_bytes = b64decode(swap_tx_b64)
                swap_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
                # assinatura com solders - use to_bytes_versioned
                signature = payer.sign_message(to_bytes_versioned(swap_tx.message))
                signed_tx = VersionedTransaction.populate(swap_tx.message, [signature])

                tx_opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
                # send_raw_transaction espera bytes
                tx_signature = solana_client.send_raw_transaction(bytes(signed_tx), opts=tx_opts).value

                logger.info(f"Transação enviada: {tx_signature}")

                # tentativa de confirmação
                try:
                    solana_client.confirm_transaction(tx_signature, commitment="confirmed")
                    logger.info(f"Transação confirmada: https://solscan.io/tx/{tx_signature}")
                    return str(tx_signature) # Success
                except Exception as confirm_e:
                    logger.error(f"Transação enviada, mas falha na confirmação: {confirm_e}")
                    # Confirmation failed, but transaction might still go through.
                    # For simplicity here, we treat this as a failure to trigger retry or main loop retry.
                    pass # Let the outer loop handle retries if tx_sig is None

            except Exception as e:
                logger.error(f"Falha na transação (tentativa {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5) # Wait before retrying

    logger.error(f"Falha na transação após {max_retries} tentativas.")
    await send_telegram_message(f"⚠️ Falha na transação após {max_retries} tentativas: {e}")
    return None # Return None if all retries fail


async def calculate_dynamic_slippage(pair_address):
    logger.info(f"Calculando slippage dinâmico para {pair_address} com base na volatilidade...")
    df = await fetch_geckoterminal_ohlcv(pair_address, "1m", limit=5)
    if df is None or df.empty or len(df) < 5:
        logger.warning("Dados insuficientes. Usando slippage padrão (5.0%).")
        return 500, 0 # Return default slippage and 0 volatility
    price_range = df['high'].max() - df['low'].min()
    volatility = (price_range / df['low'].min()) * 100 if df['low'].min() > 0 else 0
    if volatility > 10.0:
        slippage_bps = 500 # 5%
    else:
        slippage_bps = 200  # 2%
    logger.info(f"Volatilidade ({volatility:.2f}%). Slippage definido para {slippage_bps/100:.2f}%.")
    return slippage_bps, volatility # Return slippage and volatility

# ---------------- Funções de dados reais (Dexscreener / Geckoterminal) ----------------
async def get_pair_details(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()

            # A API retorna um array de pares, mesmo que você procure por um só
            pair_data = res.json().get('pairs', [None])[0]
            if not pair_data:
                return None

            # Retorna o dicionário completo que a função analyze_and_score_coin espera
            # Extraímos todos os dados necessários aqui
            return pair_data

    except Exception as e:
        # Se a requisição falhar, a função retorna None, o que já é tratado no loop
        return None

async def fetch_geckoterminal_ohlcv(pair_address, timeframe, limit=60):
    # timeframe map (apenas 1m implementado)
    timeframe_map = {"1m": "minute", "5m": "minute"}  # para 5m tratamos agregando candles mais tarde se necessário
    gt_timeframe = timeframe_map.get(timeframe, "minute")
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/{gt_timeframe}?aggregate=1&limit={limit}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            data = res.json()
            if data.get('data') and data['data'].get('attributes', {}).get('ohlcv_list'):
                ohlcv = data['data']['attributes']['ohlcv_list']
                df = pd.DataFrame(ohlcv, columns=['ts','o','h','l','c','v'])
                df[['o','h','l','c','v']] = df[['o','h','l','c','v']].apply(pd.to_numeric)
                df.rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'}, inplace=True)
                return df.tail(limit).reset_index(drop=True)
    except Exception:
        return None

async def fetch_dexscreener_real_time_price(pair_address):
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=5.0)
            res.raise_for_status()
            pair_data = res.json().get('pair')
            if pair_data:
                # priceNative and priceUsd available depending on the pair
                native = pair_data.get('priceNative')
                usd = pair_data.get('priceUsd')
                try:
                    return float(native if native is not None else 0), float(usd if usd is not None else 0)
                except:
                    return None, None
            return None, None
    except Exception:
        return None, None

async def is_pair_quotable_on_jupiter(pair_details):
    if not pair_details: return False
    test_amount_wei = 10000
    # Atualizado para o novo endpoint da Jupiter
    url = f"https://lite-api.jup.ag/swap/v1/quote?inputMint={pair_details['quoteToken']['address']}&outputMint={pair_details['baseToken']['address']}&amount={test_amount_wei}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            return res.status_code == 200
    except Exception:
        return False


# ---------------- Ordem: BUY / SELL (usando seu código) ----------------
async def execute_buy_order(amount, price, pair_details, manual=False, reason="Sinal da Estratégia"):
    global in_position, entry_price, sell_fail_count, buy_fail_count

    if in_position:
        # In manual mode, allow adding to position via /buy
        logger.info("Já em posição, executando compra adicional.")
        # For simplicity, we don't update entry_price for add-on buys.
        # A more complex bot would calculate an average entry price.
    else:
        logger.info("Iniciando primeira ordem de compra.")


    logger.info(f"Iniciando swap de {amount} SOL para {pair_details['baseToken']['symbol']} ao preço de {price}")

    # Calculate slippage based on volatility (kept as per user request)
    slippage_bps, volatility = await calculate_dynamic_slippage(pair_details['pairAddress'])


    # Assuming quoteToken is always SOL and baseToken is the target token
    tx_sig = await execute_swap("So11111111111111111111111111111111111111112", pair_details['baseToken']['address'], amount, 9, slippage_bps)

    if tx_sig:
        if not in_position: # Only set entry price and timestamp for the first buy
            in_position = True
            entry_price = price # Set entry_price on successful buy (in USD)
            automation_state["position_opened_timestamp"] = time.time()

        sell_fail_count = 0
        buy_fail_count = 0
        log_message = (f"✅ COMPRA REALIZADA: {amount} SOL para {pair_details['baseToken']['symbol']}\n"
                       f"Motivo: {reason}\n"
                       f"Entrada (USD): ${entry_price:.10f} | Alvo (USD): ${entry_price * (1 + parameters['take_profit_percent']/100):.10f} | "
                       f"Stop (USD): ${entry_price * (1 - parameters['stop_loss_percent']/100):.10f}\n"
                       f"Slippage Usado: {slippage_bps/100:.2f}%\n"
                       f"Taxa de Prioridade: {parameters.get('priority_fee')} micro-lamports\n"
                       f"https://solscan.io/tx/{tx_sig}")
        logger.info(log_message)
        await send_telegram_message(log_message)
    else:
        buy_fail_count += 1
        logger.error(f"FALHA NA EXECUÇÃO da compra para {pair_details['baseToken']['symbol']}. Tentativa {buy_fail_count}/10.")
        if buy_fail_count >= 10:
            logger.error(f"Limite de falhas de compra atingido para {pair_details['baseToken']['symbol']}.")
            await send_telegram_message(f"❌ FALHA NA EXECUÇÃO da compra para **{pair_details['baseToken']['symbol']}**. Limite atingido.")
            buy_fail_count = 0

async def execute_sell_order(reason="", sell_price=None):
    global in_position, entry_price, sell_fail_count, buy_fail_count
    if not in_position: return

    pair_details = automation_state.get('current_target_pair_details', {})
    symbol = pair_details.get('baseToken', {}).get('symbol', 'TOKEN')
    pair_address = pair_details.get('pairAddress')

    if not pair_details or not pair_address:
         logger.error("execute_sell_order: pair_details ou pair_address não definidos no estado. Não é possível vender.")
         # Increment fail count and notify, but don't abandon position yet
         sell_fail_count += 1
         await send_telegram_message(f"⚠️ Erro interno: detalhes do par não definidos. A venda falhou. Tentativa {sell_fail_count}/100. O bot tentará novamente.")
         return # Return without changing position state


    logger.info(f"EXECUTANDO ORDEM DE VENDA de {symbol}. Motivo: {reason}")
    try:
        token_mint_pubkey = Pubkey.from_string(pair_details['baseToken']['address'])
        ata_address = get_associated_token_address(payer.pubkey(), token_mint_pubkey)

        balance_response = solana_client.get_token_account_balance(ata_address)

        if hasattr(balance_response, 'value'):
            token_balance_data = balance_response.value
        else:
            logger.error(f"Erro ao obter saldo do token {symbol}: Resposta RPC inválida.")
            # Increment fail count and notify, but don't abandon position yet
            sell_fail_count += 1
            await send_telegram_message(f"⚠️ Erro ao obter saldo do token {symbol}. A venda falhou. Tentativa {sell_fail_count}/100. O bot tentará novamente.")
            return # Return without changing position state

        amount_to_sell = token_balance_data.ui_amount
        if amount_to_sell is None or amount_to_sell == 0:
            logger.warning("Tentativa de venda com saldo zero, resetando posição.")
            # Reset position state as there's nothing to sell
            in_position = False
            entry_price = 0.0
            automation_state["position_opened_timestamp"] = 0
            automation_state["current_target_pair_address"] = None # Reset target after selling
            sell_fail_count = 0
            return

        slippage_bps, _ = await calculate_dynamic_slippage(pair_details['pairAddress']) # Volatility not needed for sell slippage
        tx_sig = await execute_swap(pair_details['baseToken']['address'], pair_details['quoteToken']['address'], amount_to_sell, token_balance_data.decimals, slippage_bps)

        if tx_sig:
            # Calculate P/L using entry_price (USD) and sell_price (USD)
            profit_loss_percent = 0.0
            if entry_price is not None and entry_price > 0 and sell_price is not None:
                 profit_loss_percent = ((sell_price - entry_price) / entry_price) * 100

            log_message = (f"🛑 VENDA REALIZADA: {symbol}\n"
                           f"Motivo: {reason}\n"
                           f"Lucro/Prejuízo: {profit_loss_percent:.2f}%\n"
                           f"Entrada (USD): ${entry_price:.10f} | Saída (USD): ${sell_price:.10f}\n" # Corrected display
                           f"Slippage Usado: {slippage_bps/100:.2f}%\n"
                           f"Taxa de Prioridade: {parameters.get('priority_fee')} micro-lamports\n"
                           f"https://solscan.io/tx/{tx_sig}")
            logger.info(log_message)
            await send_telegram_message(log_message)

            # Successfully sold, reset position and fail counts
            in_position = False
            entry_price = 0.0
            automation_state["position_opened_timestamp"] = 0

            # No penalty box logic on sell
            automation_state["current_target_pair_address"] = None # Reset target after selling


            sell_fail_count = 0
            buy_fail_count = 0
        else:
            # This block is reached if execute_swap returns None after retries
            logger.error(f"FALHA NA VENDA do token {symbol} após retentativas. Tentativa {sell_fail_count+1}/100.")
            sell_fail_count += 1
            await send_telegram_message(f"❌ FALHA NA VENDA do token {symbol} após retentativas. Tentativa {sell_fail_count}/100. O bot tentará novamente.")

            if sell_fail_count >= 100: # Check limit AFTER incrementing
                logger.error(f"ATINGIDO LIMITE DE {sell_fail_count} FALHAS DE VENDA. RESETANDO POSIÇÃO.")
                await send_telegram_message(f"⚠️ Limite de {sell_fail_count} falhas de venda para **{symbol}** atingido. Posição abandonada.")
                # Abandon position on hitting the limit
                in_position = False
                entry_price = 0.0
                automation_state["position_opened_timestamp"] = 0
                automation_state["current_target_pair_address"] = None
                sell_fail_count = 0 # Reset fail count after abandoning

    except Exception as e:
        logger.error(f"Erro crítico ao vender {symbol}: {e}")
        # Increment fail count and notify, but don't abandon position immediately on critical error
        sell_fail_count += 1
        await send_telegram_message(f"⚠️ Erro crítico ao vender {symbol}: {e}. Tentativa {sell_fail_count}/100. O bot permanecerá em posição.")
        # Abandon position only if critical errors also hit the limit
        if sell_fail_count >= 100:
             logger.error(f"ATINGIDO LIMITE DE {sell_fail_count} ERROS EM manage_position. RESETANDO POSIÇÃO.")
             await send_telegram_message(f"⚠️ Limite de {sell_fail_count} erros em manage_position para **{symbol}** atingido. Posição abandonada.")
             in_position = False
             entry_price = 0.0
             automation_state["position_opened_timestamp"] = 0
             automation_state["current_target_pair_address"] = None
             sell_fail_count = 0 # Reset fail count after abandoning


# ---------------- Estratégia velocity / momentum & adaptive timeout ----------------
import time
import asyncio

# REMOVED check_velocity_strategy function


async def manage_position():
    """Gerencia a posição de trade, checando Take Profit, Stop Loss."""
    global in_position, automation_state, entry_price, sell_fail_count

    pair_details = automation_state.get("current_target_pair_details")

    if not in_position or not pair_details:
        return

    target_address = pair_details.get('pairAddress')
    symbol = pair_details.get('baseToken', {}).get('symbol', 'N/A')
    buy_price = entry_price # Use entry_price from global state (in USD)
    position_opened_timestamp = automation_state.get("position_opened_timestamp", 0)

    if buy_price is None or buy_price == 0.0: # Check if entry_price is set and not zero
         logger.error(f"manage_position: entry_price não definido ou é zero ({buy_price}), não é possível gerenciar a posição.")
         # Consider adding logic here to exit the position if entry_price is invalid
         return

    # Obtém os valores de Take Profit e Stop Loss da configuração
    take_profit_percentage = parameters.get("take_profit_percent")
    stop_loss_percentage = parameters.get("stop_loss_percent")

    if take_profit_percentage is None or stop_loss_percentage is None:
        logger.error("Parâmetros de Take Profit ou Stop Loss não definidos. Não é possível gerenciar a posição.")
        return

    try:
        # Puxa o preço atual da moeda
        # Use fetch_dexscreener_real_time_price que retorna priceNative e priceUsd
        price_native, current_price_usd = await fetch_dexscreener_real_time_price(target_address)

        # Decidir qual preço usar para TP/SL. Se a entrada foi em USD, usar USD. Se foi em SOL (native), usar native.
        # Assumindo que a entrada (entry_price) é em USD (baseado nos logs de compra), usamos current_price_usd
        current_price = current_price_usd
        if current_price is None or current_price == 0:
            logger.warning(f"Não foi possível obter o preço atual em USD para {symbol}.")
            # Increment sell_fail_count here as well if price fetch fails
            sell_fail_count += 1
            if sell_fail_count >= 100: # Check if price fetch failures exceed limit
                logger.error(f"ATINGIDO LIMITE DE {sell_fail_count} FALHAS AO OBTER PREÇO. RESETANDO POSIÇÃO.")
                await send_telegram_message(f"⚠️ Limite de {sell_fail_count} falhas ao obter preço para **{symbol}** atingido. Posição abandonada.")
                in_position = False
                entry_price = 0.0
                automation_state["position_opened_timestamp"] = 0
                automation_state["current_target_pair_address"] = None # Reset target after abandoning
                sell_fail_count = 0 # Reset fail count after abandoning
            return

        # Calcula os preços de TP e SL com base no preço de compra (em USD)
        take_profit_price = buy_price * (1 + take_profit_percentage / 100)
        stop_loss_price = buy_price * (1 - stop_loss_percentage / 100)


        # Checa as condições de venda (TP, SL)
        if current_price >= take_profit_price:
            msg = f"🟢 **TAKE PROFIT ATINGIDO!** Vendendo **{symbol}** com lucro. Valor Corrente (USD): ${current_price:.10f} Take Profit (USD): ${take_profit_price:.10f}"
            logger.info(msg.replace("**", ""))
            await send_telegram_message(msg)
            await execute_sell_order(reason="Take Profit Atingido", sell_price=current_price)
            # execute_sell_order will handle state reset and penalty if successful

        elif current_price <= stop_loss_price:
            msg = f"🔴 **STOP LOSS ATINGIDO!** Vendendo **{symbol}** para limitar o prejuízo. Valor Corrente (USD): ${current_price:.10f} Stop Loss (USD): ${stop_loss_price:.10f}"
            logger.info(msg.replace("**", ""))
            await send_telegram_message(msg)
            await execute_sell_order(reason="Stop Loss Atingido", sell_price=current_price)
            # execute_sell_order will handle state reset and penalty if successful

        else:
            # Continua monitorando a posição
            logger.info(f"Monitorando {symbol} | Preço atual (USD): ${current_price:,.8f} | TP: ${take_profit_price:,.8f} | SL: ${stop_loss_price:,.8f}")
            # Reset sell_fail_count if monitoring is successful (price fetched)
            sell_fail_count = 0


    except Exception as e:
        logger.error(f"Erro em manage_position: {e}", exc_info=True)
        # Increment sell_fail_count for unhandled exceptions in manage_position
        sell_fail_count += 1
        await send_telegram_message(f"⚠️ Erro em manage_position para {symbol}: {e}. Tentativa {sell_fail_count}/100. O bot permanecerá em posição.")
        if sell_fail_count >= 100: # Check limit AFTER incrementing
             logger.error(f"ATINGIDO LIMITE DE {sell_fail_count} ERROS EM manage_position. RESETANDO POSIÇÃO.")
             await send_telegram_message(f"⚠️ Limite de {sell_fail_count} erros em manage_position para **{symbol}** atingido. Posição abandonada.")
             in_position = False
             entry_price = 0.0
             automation_state["position_opened_timestamp"] = 0
             automation_state["current_target_pair_address"] = None
             sell_fail_count = 0 # Reset fail count after abandoning


# ---------------- Loop autônomo completo ----------------
async def get_pair_details_by_token_address(token_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(url, timeout=10.0)
            res.raise_for_status()
            token_data = res.json().get('pairs')
            if token_data:
                # Find a suitable pair, e.g., the first one or one with sufficient liquidity
                for pair in token_data:
                     # Assuming we are trading against SOL (quoteToken is SOL)
                     if pair.get('quoteToken', {}).get('address') == 'So11111111111111111111111111111111111111112':
                         return pair
                return None # No SOL pair found
            return None # No pairs found for the token

    except Exception as e:
        logger.error(f"Erro ao buscar detalhes do par pelo endereço do token {token_address}: {e}")
        return None


async def autonomous_loop():
    """O loop principal que executa a estratégia de trade de forma autônoma, com estados de operação claros."""
    global automation_state, in_position

    logger.info("Loop autônomo iniciado.")
    automation_state["is_running"] = True # Ensure is_running is True when loop starts

    # Fetch pair details for the target token once when the bot starts
    target_token_address = parameters.get("target_token_address")
    if not target_token_address:
        logger.error("Endereço do token alvo não definido. Use /set para configurar.")
        await send_telegram_message("❌ Loop autônomo não iniciado: Endereço do token alvo não definido. Use `/set <ENDERECO_TOKEN> <VALOR_SOL> <STOP_LOSS_%> <TAKE_PROFIT_%> [PRIORITY_FEE]` para configurar.")
        automation_state["is_running"] = False
        return

    # Fetch pair details for the target token using the token address
    pair_details = await get_pair_details_by_token_address(target_token_address)
    if not pair_details:
        logger.error(f"Não foi possível obter detalhes para o token alvo: {target_token_address}. Verifique o endereço.")
        await send_telegram_message(f"❌ Loop autônomo não iniciado: Não foi possível obter detalhes para o token alvo: `{target_token_address}`. Verifique o endereço.")
        automation_state["is_running"] = False
        return

    automation_state["current_target_pair_details"] = pair_details
    automation_state["current_target_pair_address"] = pair_details.get('pairAddress')
    automation_state["current_target_symbol"] = pair_details.get('baseToken', {}).get('symbol', 'N/A')
    automation_state["target_selected_timestamp"] = time.time() # Set timestamp when target is confirmed

    msg = f"🎯 **Alvo Definido:** {automation_state['current_target_symbol']} ({automation_state['current_target_pair_address']}). Iniciando monitoramento..."
    logger.info(msg.replace("**", ""))
    await send_telegram_message(msg)


    while automation_state.get("is_running", False):
        try:
            # ------------------------------------------------------------------
            # ESTADO: MONITORAMENTO E GERENCIAMENTO (Alvo selecionado, aguardando para comprar ou em posição)
            # ------------------------------------------------------------------
            # In manual mode, we only manage the position if we are in one.
            # Manual buy is done via command.
            if in_position:
                await manage_position()
            else:
                 # If not in position, just wait for manual buy command.
                 logger.info("Bot is idle, waiting for manual buy command.")

            await asyncio.sleep(15) # Adjust sleep time as needed


        except asyncio.CancelledError:
            logger.info("Loop autônomo cancelado.")
            break
        except Exception as e:
            logger.error(f"Erro crítico no loop autônomo: {e}", exc_info=True)
            await asyncio.sleep(60)



# ---------------- Comandos Telegram ----------------
async def start(update, context):
    await update.effective_message.reply_text(
        'Olá! Bot sniper iniciado em modo manual.\nUse `/set <ENDERECO_TOKEN> <STOP_LOSS_%> <TAKE_PROFIT_%> [PRIORITY_FEE]` para definir o token e parâmetros. Use `/run` para iniciar o monitoramento da posição (após a compra inicial com /buy). Use `/buy <VALOR_SOL>` para comprar ou `/sell` para vender.',
        parse_mode='Markdown'
    )

async def set_params(update, context):
    global automation_state # Removed in_position and entry_price from global

    if automation_state.get("is_running", False):
        await update.effective_message.reply_text("Pare o bot com /stop antes de alterar os parâmetros."); return
    try:
        args = context.args
        # Adjusted expected number of arguments
        if len(args) < 3:
             await update.effective_message.reply_text("⚠️ Formato incorreto. Uso: `/set <ENDERECO_TOKEN> <STOP_LOSS_%> <TAKE_PROFIT_%> [PRIORITY_FEE]`", parse_mode='Markdown')
             return

        target_token_address = args[0]
        # Removed amount from here
        stop_loss = float(args[1])
        take_profit = float(args[2])
        priority_fee = int(args[3]) if len(args) > 3 else 2000000 # Adjusted index

        # Optional: Basic validation for token address format
        # try:
        #     Pubkey.from_string(target_token_address)
        # except Exception:
        #     await update.effective_message.reply_text("⚠️ Endereço do token inválido.", parse_mode='Markdown'); return

        # Store parameters, excluding amount
        parameters.update(
            target_token_address=target_token_address,
            stop_loss_percent=stop_loss,
            take_profit_percent=take_profit,
            priority_fee=priority_fee
        )

        # Fetch pair details for the target token immediately
        pair_details = await get_pair_details_by_token_address(target_token_address)
        if not pair_details:
            logger.error(f"Não foi possível obter detalhes para o token alvo: {target_token_address}. Verifique o endereço.")
            await send_telegram_message(f"❌ Não foi possível obter detalhes para o token alvo: `{target_token_address}`. Verifique o endereço.")
            # Do not set target_pair_details if fetch failed
            automation_state["current_target_pair_details"] = None
            automation_state["current_target_pair_address"] = None
            automation_state["current_target_symbol"] = None
            return

        # Update automation_state with the target details
        automation_state["current_target_pair_details"] = pair_details
        automation_state["current_target_pair_address"] = pair_details.get('pairAddress')
        automation_state["current_target_symbol"] = pair_details.get('baseToken', {}).get('symbol', 'N/A')
        automation_state["target_selected_timestamp"] = time.time() # Set timestamp when target is confirmed

        await update.effective_message.reply_text(
            f"✅ Parâmetros definidos para o modo manual:\n"
            f"Token Alvo: `{target_token_address}`\n"
            # Removed Valor de Compra Inicial display
            f"Stop Loss: {stop_loss}%\n"
            f"Take Profit: {take_profit}%\n"
            f"Priority Fee: {priority_fee}\n\n"
            "Use `/buy <VALOR_SOL>` para realizar a primeira compra e `/run` para iniciar o monitoramento de TP/SL.",
            parse_mode='Markdown'
        )

    except ValueError:
        await update.effective_message.reply_text("⚠️ Valores numéricos inválidos. Verifique STOP_LOSS_%, TAKE_PROFIT_% e PRIORITY_FEE.", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Erro no comando /set: {e}")
        await update.effective_message.reply_text(f"⚠️ Erro ao definir parâmetros: {e}\nUso: `/set <ENDERECO_TOKEN> <STOP_LOSS_%> <TAKE_PROFIT_%> [PRIORITY_FEE]`", parse_mode='Markdown')


async def run_bot(update, context):
    """Inicia o loop de trade autônomo (modo manual)."""
    global automation_state, in_position, entry_price
    if automation_state.get("is_running", False):
        await update.effective_message.reply_text("✅ O bot já está em execução."); return

    # Ensure target token and parameters are set before running
    if not parameters.get("target_token_address") or parameters.get("stop_loss_percent") is None or parameters.get("take_profit_percent") is None:
         await update.effective_message.reply_text("⚠️ Parâmetros não definidos. Use `/set <ENDERECO_TOKEN> <STOP_LOSS_%> <TAKE_PROFIT_%> [PRIORITY_FEE]` antes de iniciar o monitoramento.", parse_mode='Markdown')
         return

    # Ensure pair details are loaded if /set was used but /run wasn't called immediately
    if not automation_state.get("current_target_pair_details"):
         target_token_address = parameters.get("target_token_address")
         pair_details = await get_pair_details_by_token_address(target_token_address)
         if not pair_details:
             logger.error(f"Não foi possível obter detalhes para o token alvo: {target_token_address}. Não é possível iniciar o monitoramento.")
             await send_telegram_message(f"❌ Loop autônomo não iniciado: Não foi possível obter detalhes para o token alvo: `{target_token_address}`. Verifique o endereço.")
             automation_state["is_running"] = False
             return

         automation_state["current_target_pair_details"] = pair_details
         automation_state["current_target_pair_address"] = pair_details.get('pairAddress')
         automation_state["current_target_symbol"] = pair_details.get('baseToken', {}).get('symbol', 'N/A')
         automation_state["target_selected_timestamp"] = time.time() # Set timestamp when target is confirmed


    # Check if in_position is True. If not, inform the user to use /buy first.
    if not in_position:
        await update.effective_message.reply_text("⚠️ Nenhuma posição aberta. Use `/buy <VALOR_SOL>` para comprar antes de iniciar o monitoramento de TP/SL.", parse_mode='Markdown')
        return


    automation_state["is_running"] = True
    automation_state["task"] = asyncio.create_task(autonomous_loop())

    logger.info("Bot de trade autônomo iniciado.")
    await send_telegram_message(
        "🚀 Bot de trade autônomo iniciado em modo manual!\n"
        f"Monitorando a posição aberta no token alvo: `{automation_state.get('current_target_token_address', 'N/A')}`\n"
        "O bot irá gerenciar Take Profit e Stop Loss automaticamente. Use os comandos `/buy <VALOR_SOL>` para compras adicionais ou `/sell` para vender manualmente."
    )

async def stop_bot(update, context):
    """Para o loop de trade autônomo e cancela a tarefa em execução."""
    global automation_state, in_position, entry_price

    if not automation_state.get("is_running", False):
        await update.effective_message.reply_text("O bot já está parado."); return

    # CORREÇÃO: Desliga o bot usando a variável de estado correta
    automation_state["is_running"] = False

    # Cancela a tarefa asyncio if it exists
    if "task" in automation_state and automation_state["task"]:
        automation_state["task"].cancel()

    if in_position:
        # Fetch current price to calculate P/L before selling
        pair_details = automation_state.get('current_target_pair_details', {})
        pair_address = pair_details.get('pairAddress')
        _, current_price_usd = await fetch_dexscreener_real_time_price(pair_address)
        await execute_sell_order(reason="Parada manual do bot", sell_price=current_price_usd)

    # Limpa o estado para um reinício limpo
    in_position = False
    entry_price = 0.0 # Explicitly reset entry_price
    automation_state.update(
        current_target_pair_address=None,
        current_target_symbol=None,
        current_target_pair_details=None,
        position_opened_timestamp=0,
        target_selected_timestamp=0,
        checking_volatility=False,
        volatility_check_start_time=0,
        # Removed penalty_box, discovered_pairs, took_profit_pairs
    )
    # Also reset parameters related to the target token
    parameters.update(
        target_token_address=None
    )


    logger.info("Bot de trade parado.")
    await update.effective_message.reply_text("🛑 Bot parado. Todas as tarefas e posições foram finalizadas.")

async def manual_buy(update, context):
    global in_position, entry_price, automation_state
    if not automation_state.get("is_running", False):
        await update.effective_message.reply_text("⚠️ O bot precisa estar em execução para executar ordens. Use /run primeiro para iniciar o monitoramento (após definir o token com /set).")
        return
    if not automation_state.get("current_target_pair_details"):
        await update.effective_message.reply_text("⚠️ O bot ainda não carregou os detalhes do token alvo. Certifique-se de ter usado /set para definir o token alvo primeiro.")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            await update.effective_message.reply_text("⚠️ O valor da compra deve ser positivo.")
            return
        pair_details = automation_state["current_target_pair_details"]
        # Use priceUsd for manual buy price display
        _, price_usd = await fetch_dexscreener_real_time_price(pair_details['pairAddress'])
        if price_usd is not None:
            await update.effective_message.reply_text(f"Forçando compra manual de {amount} SOL em {pair_details['baseToken']['symbol']}...")
            # Pass the price in USD to execute_buy_order
            # Set manual=True so it skips any non-manual triggers if they were re-added
            await execute_buy_order(amount, price_usd, pair_details, manual=True, reason="Compra Manual Forçada")
            # entry_price is set inside execute_buy_order *only if it's the first position*
            # If already in position, entry_price is NOT updated here.
        else:
            await update.effective_message.reply_text("⚠️ Não foi possível obter o preço atual para a compra.")
    except (IndexError, ValueError):
        await update.effective_message.reply_text("⚠️ Formato incorreto. Use: `/buy <VALOR_SOL>`", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Erro no comando /buy: {e}")
        await update.effective_message.reply_text(f"⚠️ Erro ao executar compra manual: {e}")

async def manual_sell(update, context):
    global in_position, entry_price
    if not automation_state.get("is_running", False):
        await update.effective_message.reply_text("⚠️ O bot precisa estar em execução para executar ordens. Use /run primeiro para iniciar o monitoramento.")
        return
    if not in_position:
        await update.effective_message.reply_text("⚠️ Nenhuma posição aberta para vender.")
        return
    pair_details = automation_state.get('current_target_pair_details', {})
    pair_address = pair_details.get('pairAddress')
    _, current_price_usd = await fetch_dexscreener_real_time_price(pair_address)
    await update.effective_message.reply_text("Forçando venda manual da posição atual...")
    await execute_sell_order(reason="Venda Manual Forçada", sell_price=current_price_usd)
    # in_position and entry_price are reset inside execute_sell_order if successful


# ---------------- Main ----------------
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
    logger.info("Bot do Telegram iniciado e aguardando comandos...")
    application.run_polling()

if __name__ == '__main__':
    main()
