import logging
import requests
import json
import asyncio
import os
from web3 import Web3
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from datetime import datetime

# Настроим логирование
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger()

# Токен Telegram-бота
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("Переменная окружения TELEGRAM_TOKEN не установлена.")

# Словарь для хранения задач
user_tasks = {}

# Загрузка ABI-файлов и контрактов
try:
    with open("BMX_wMLT_abi.json", "r") as abi_file:
        bmx_wmlt_abi = json.load(abi_file)
    with open("USDC_wMLT_abi.json", "r") as abi_file:
        usdc_wmlt_abi = json.load(abi_file)
except Exception as e:
    logger.error(f"Ошибка при загрузке ABI-файлов: {e}")
    raise


# Подключение к BASE
rpc_url_base = "https://api.odos.xyz/sor/quote/v2"
logger.info("BASE: connected")

# Подключение к MODE
rpc_url_mode = "https://mainnet.mode.network"
web3_mode = Web3(Web3.HTTPProvider(rpc_url_mode))

if not web3_mode.is_connected():
    raise ConnectionError("Ошибка подключения к RPC MODE.")
logger.info("MODE: connected")


# BASE реквизиты
bmx_base_address = "0x548f93779fBC992010C07467cBaf329DD5F059B7"
usdc_base_address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
bmx_base_amount = "100000000000000000000"

# MODE реквизиты
bmx_mode_address = web3_mode.to_checksum_address("0x66eed5ff1701e6ed8470dc391f05e27b1d0657eb")
usdc_mode_address = web3_mode.to_checksum_address("0xd988097fb8612cc24eeC14542bC03424c656005f")
bmx_wmlt_contract_address = web3_mode.to_checksum_address("0x70f531F133C7De52F0b06F193D862f5a8f17A0cF")
usdc_wmlt_contract_address = web3_mode.to_checksum_address("0x9b44Ddbe036DC8e3bfF1Cb703E1E07c96164532D")

bmx_wmlt_contract = web3_mode.eth.contract(address=bmx_wmlt_contract_address, abi=bmx_wmlt_abi)
usdc_wmlt_contract = web3_mode.eth.contract(address=usdc_wmlt_contract_address, abi=usdc_wmlt_abi)

wmlt_address = web3_mode.to_checksum_address("0x8b2eea0999876aab1e7955fe01a5d261b570452c")


# Хелпер для обработки outAmounts
def validate_out_amounts(response_json, scale, log_prefix):
    try:
        out_amounts = response_json.get("outAmounts", [])
        if out_amounts:
            value = float(out_amounts[0]) / (10**scale)
            return value
        else:
            logger.error(f"{log_prefix}: outAmounts is empty or not found.")
            return None
    except Exception as e:
        logger.error(f"{log_prefix}: Error processing outAmounts: {e}")
        return None


# Функция для получения цены BMX в BASE
def get_base_price():
    try:
        quote_request_body = {
            "chainId": 8453,
            "inputTokens": [{"tokenAddress": bmx_base_address, "amount": bmx_base_amount}],
            "outputTokens": [{"tokenAddress": usdc_base_address, "proportion": 1}],
            "slippageLimitPercent": 1,
        }
        response = requests.post(
            rpc_url_base,
            headers={"Content-Type": "application/json"},
            data=json.dumps(quote_request_body)
        )
        if response.status_code == 200:
            quote = response.json()
            return validate_out_amounts(quote, 6, "get_base_price")
        else:
            logger.error(f"get_base_price ошибка API BASE: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"get_base_price исключение при запросе BASE API: {e}")
    return None


# Функция для получения цены BMX в MODE
def get_mode_price():
    try:
        bmx_mode_amount = 100 * (10**18)
        wmlt_received = bmx_wmlt_contract.functions.getAmountOut(bmx_mode_amount, bmx_mode_address).call()
        usdc_received = usdc_wmlt_contract.functions.getRedeemAmountWrappedBLT(usdc_mode_address, wmlt_received, False).call()
        return usdc_received / (10**6)
    except Exception as e:
        logger.error(f"Ошибка при вызове get_mode_price: {e}")
    return None


# Основная логика проверки арбитража
def calculate_arbitrage(base_price, mode_price):
    try:
        # Арбитраж BASE → MODE
        usdc_after_fee_base = int((base_price - 1) * (10**6))  # Преобразуем в uint256
        wmlt_received = usdc_wmlt_contract.functions.getMintAmountWrappedBLT(usdc_mode_address, usdc_after_fee_base).call()
        if not wmlt_received:
            logger.error("calculate_arbitrage: wmlt_received is None")
            return None, None
        bmx_received = bmx_wmlt_contract.functions.getAmountOut(wmlt_received, wmlt_address).call()
        bmx_diff_base_to_mode = bmx_received / (10**18) - 100

        # Арбитраж MODE → BASE
        usdc_after_fee_mode = int((mode_price - 1) * (10**6))  # Преобразуем в uint256
        quote_request_body = {
            "chainId": 8453,
            "inputTokens": [{"tokenAddress": usdc_base_address, "amount": str(usdc_after_fee_mode)}],
            "outputTokens": [{"tokenAddress": bmx_base_address, "proportion": 1}],
            "slippageLimitPercent": 1,
        }
        response = requests.post(
            rpc_url_base,
            headers={"Content-Type": "application/json"},
            data=json.dumps(quote_request_body)
        )
        if response.status_code != 200:
            logger.error(f"Ошибка API ODOS: {response.status_code} - {response.text}")
            return None, None

        quote = response.json()
        bmx_received_mode = validate_out_amounts(quote, 18, "calculate_arbitrage: MODE → BASE")
        if bmx_received_mode is None:
            return None, None
        bmx_diff_mode_to_base = bmx_received_mode - 100

        return bmx_diff_base_to_mode, bmx_diff_mode_to_base
    except Exception as e:
        logger.error(f"Ошибка в calculate_arbitrage: {e}")
        return None, None


# Функция оповещения об арбитраже
async def check_prices_and_notify(update: Update):
    user_id = update.message.chat_id
    last_notification_time = datetime.now()

    while True:
        base_price = get_base_price()
        mode_price = get_mode_price()

        if base_price is None or mode_price is None:
            logger.warning("Не удалось получить цены из одной или обеих сетей.")
            await asyncio.sleep(60)
            continue

        bmx_diff_base_to_mode, bmx_diff_mode_to_base = calculate_arbitrage(base_price, mode_price)

        if bmx_diff_base_to_mode is None or bmx_diff_mode_to_base is None:
            logger.warning("Не удалось рассчитать арбитражные данные.")
            await asyncio.sleep(60)
            continue

        logger.info(f"BASE → MODE: {bmx_diff_base_to_mode:.2f}, MODE → BASE: {bmx_diff_mode_to_base:.2f}")

        if bmx_diff_base_to_mode > 1:
            await update.message.reply_text(f"BASE → MODE: {bmx_diff_base_to_mode:.2f} BMX.")
        if bmx_diff_mode_to_base > 1:
            await update.message.reply_text(f"MODE → BASE: {bmx_diff_mode_to_base:.2f} BMX.")

        current_time = datetime.now()
        if (current_time - last_notification_time).total_seconds() >= 3600:
            await update.message.reply_text(
                f"Арбитражное состояние:\nBASE → MODE: {bmx_diff_base_to_mode:.2f} BMX\nMODE → BASE: {bmx_diff_mode_to_base:.2f} BMX"
            )
            last_notification_time = current_time

        await asyncio.sleep(60)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id

    if user_id in user_tasks:
        await update.message.reply_text("Бот уже запущен для вас.")
        logger.info("/start получен, Бот уже запущен")
    else:
        await update.message.reply_text("Привет! Я крипто-бот. Буду присылать тебе алерты о возможностях арбитража.")
        task = asyncio.create_task(check_prices_and_notify(update))
        user_tasks[user_id] = task
        logger.info("/start получен, Привет отправлен")

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    base_price = get_base_price()
    mode_price = get_mode_price()

    if base_price and mode_price:
        bmx_diff_base_to_mode, bmx_diff_mode_to_base = calculate_arbitrage(base_price, mode_price)
        logger.info(f"/price Арбитраж: BASE → MODE = {bmx_diff_base_to_mode:.2f}, MODE → BASE = {bmx_diff_mode_to_base:.2f}")

        # Сообщение пользователю
        await update.message.reply_text(
            f"BASE → MODE: {bmx_diff_base_to_mode:.2f} BMX\nMODE → BASE: {bmx_diff_mode_to_base:.2f} BMX"
        )
    else:
        await update.message.reply_text("Не удалось получить цены.")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id

    if user_id in user_tasks:
        user_tasks[user_id].cancel()
        del user_tasks[user_id]
        await update.message.reply_text("Бот остановлен. Вы больше не будете получать уведомления.")
        logger.info(f"/stop получен. Задача для пользователя {user_id} остановлена.")
    else:
        await update.message.reply_text("Бот не запущен для вас.")
        logger.info("/stop получен. Бот не запущен для вас")


# Основной блок запуска
if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('stop', stop))
    application.add_handler(CommandHandler('price', price))

    logger.info("Бот запущен и готов к работе.")
    application.run_polling()
