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

# Токен Telegram-бота (загружается из переменной окружения)
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("Переменная окружения TELEGRAM_TOKEN не установлена.")

# Словарь для хранения запущенных задач
user_tasks = {}


# Загрузка ABI-файлов
try:
    with open("BMX_wMLT_abi.json", "r") as abi_file:
        bmx_wmlt_abi = json.load(abi_file)
    with open("USDC_wMLT_abi.json", "r") as abi_file:
        usdc_wmlt_abi = json.load(abi_file)
except Exception as e:
    logger.error(f"Ошибка при загрузке ABI-файлов: {e}")
    raise


# Функция для получения цены BMX в USD из сети BASE
def get_base_price():
    try:
        quote_url = "https://api.odos.xyz/sor/quote/v2"
        quote_request_body = {
            "chainId": 8453,
            "inputTokens": [
                {"tokenAddress": "0x548f93779fBC992010C07467cBaf329DD5F059B7", "amount": "100000000000000000000"}
            ],
            "outputTokens": [
                {"tokenAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "proportion": 1}
            ],
            "slippageLimitPercent": 1,
        }
        
        response = requests.post(
            quote_url,
            headers={"Content-Type": "application/json"},
            data=json.dumps(quote_request_body)
        )
        
        if response.status_code == 200:
            quote = response.json()
            in_values = quote.get("inValues", [])[0]  # Цена за 100 BMX в USD
            if in_values:
                return float(in_values)
        else:
            logger.error(f"Ошибка API BASE: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Исключение при запросе BASE API: {e}")
    return None


# Функция для получения цены BMX в USD из сети MODE
def get_mode_price():
    try:
        rpc_url = "https://mainnet.mode.network"
        web3 = Web3(Web3.HTTPProvider(rpc_url))
        if not web3.is_connected():
            logger.error(f"Ошибка подключения к RPC MODE. {web3.provider._request_kwargs}")
            return None

        bmx_address = web3.to_checksum_address("0x66eed5ff1701e6ed8470dc391f05e27b1d0657eb")
        usdc_address = web3.to_checksum_address("0xd988097fb8612cc24eeC14542bC03424c656005f")
        bmx_wmlt_contract_address = web3.to_checksum_address("0x70f531F133C7De52F0b06F193D862f5a8f17A0cF")
        usdc_wmlt_contract_address = web3.to_checksum_address("0x9b44Ddbe036DC8e3bfF1Cb703E1E07c96164532D")

        bmx_wmlt_contract = web3.eth.contract(address=bmx_wmlt_contract_address, abi=bmx_wmlt_abi)
        usdc_wmlt_contract = web3.eth.contract(address=usdc_wmlt_contract_address, abi=usdc_wmlt_abi)

        bmx_amount = 100 * (10**18)
        wmlt_received = bmx_wmlt_contract.functions.getAmountOut(bmx_amount, bmx_address).call()
        usdc_received = usdc_wmlt_contract.functions.getRedeemAmountWrappedBLT(usdc_address, wmlt_received, False).call()

        return usdc_received / (10**6)
    except Exception as e:
        logger.error(f"Ошибка при получении цены MODE: {e}")
    return None


# Основная логика сравнения цен
async def check_prices_and_notify(update: Update):
    user_id = update.message.chat_id
    last_notification_time = datetime.now()  # Время последнего оповещения

    while True:
        base_price = get_base_price()  # Цена за 100 BMX в сети BASE
        mode_price = get_mode_price()  # Цена за 100 BMX в сети MODE

        if base_price is not None and mode_price is not None:
            price_diff = abs(base_price - mode_price)
            percentage_diff = (price_diff / min(base_price, mode_price)) * 100
            
            logger.info(f"Цена за 100 BMX: BASE = {base_price:.2f} USD, MODE = {mode_price:.2f} USD, Разница = {percentage_diff:.2f}%")

            # Уведомление, если разница >= 10%
            if percentage_diff >= 8.5:
                if base_price > mode_price:
                    message = f"Цена за 100 BMX в сети BASE выше на {percentage_diff:.2f}%: {base_price:.2f} USD vs {mode_price:.2f} USD."
                else:
                    message = f"Цена за 100 BMX в сети MODE выше на {percentage_diff:.2f}%: {mode_price:.2f} USD vs {base_price:.2f} USD."
                
                await update.message.reply_text(message)

            # Проверяем, прошло ли больше часа с последнего уведомления
            current_time = datetime.now()
            if (current_time - last_notification_time).total_seconds() >= 3600:
                hourly_message = (
                    f"Текущее состояние: BASE = {base_price:.2f} USD, "
                    f"MODE = {mode_price:.2f} USD, Разница = {percentage_diff:.2f}%.\n"
                    "Бот работает корректно."
                )
                await update.message.reply_text(hourly_message)
                last_notification_time = current_time
        else:
            logger.warning("Не удалось получить цены из одной или обеих сетей.")

        # Ждём 60 секунд перед следующей проверкой
        await asyncio.sleep(60)


# Функция для ручного запроса цен
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    base_price = get_base_price()  # Цена за 100 BMX в сети BASE
    mode_price = get_mode_price()  # Цена за 100 BMX в сети MODE

    if base_price is not None and mode_price is not None:
        price_diff = abs(base_price - mode_price)
        percentage_diff = (price_diff / min(base_price, mode_price)) * 100

        message = (
            f"Цена за 100 BMX:\n"
            f"- BASE: {base_price:.2f} USD\n"
            f"- MODE: {mode_price:.2f} USD\n"
            f"- Разница: {percentage_diff:.2f}%\n"
        )
        if percentage_diff >= 8.5:
            if base_price > mode_price:
                message += "Цена BMX в сети BASE выше."
            else:
                message += "Цена BMX в сети MODE выше."
        else:
            message += "Разница не превышает 8.5%."
    else:
        message = "Не удалось получить цены из одной или обеих сетей."

    await update.message.reply_text(message)


# Функция для старта бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id

    if user_id in user_tasks:
        await update.message.reply_text("Бот уже запущен для вас.")
    else:
        await update.message.reply_text("Привет. Я крипто-бот. Буду присылать тебе крипто-алёрты.")
        task = asyncio.create_task(check_prices_and_notify(update))
        user_tasks[user_id] = task


# Функция для остановки уведомлений
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id

    if user_id in user_tasks:
        user_tasks[user_id].cancel()
        del user_tasks[user_id]
        await update.message.reply_text("Бот остановлен. Вы больше не будете получать уведомления.")
        logger.info(f"Задача для пользователя {user_id} остановлена.")
    else:
        await update.message.reply_text("Бот не запущен для вас.")


# Функция для отправки сообщения в Telegram
async def send_telegram_message(update: Update, message: str):
    await update.message.reply_text(message)


if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('stop', stop))
    application.add_handler(CommandHandler('price', price))

    logger.info("Бот запущен и готов к работе.")
    application.run_polling()
