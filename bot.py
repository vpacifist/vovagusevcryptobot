import logging
import requests
import json
import asyncio
import os
import httpcore
from web3 import Web3
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv


# Логирование
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger()


# Токен Telegram-бота
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is not set. Please configure it in the environment.")


price_check_task = None  # Глобальная задача
hourly_alert_task = None
last_arbitrage_result = {"base_to_mode": None, "mode_to_base": None}
allowed_users = {116350148, 63853863}  # Только эти ID могут пользоваться ботом


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
            data=json.dumps(quote_request_body),
            timeout=10
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
            data=json.dumps(quote_request_body),
            timeout=10
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
async def check_prices_and_notify():
    global last_arbitrage_result

    while True:
        try:
            base_price = get_base_price()
            mode_price = get_mode_price()

            if base_price is None or mode_price is None:
                logger.warning("Не удалось получить цены из одной или обеих сетей.")
                await asyncio.sleep(15)
                continue

            bmx_diff_base_to_mode, bmx_diff_mode_to_base = calculate_arbitrage(base_price, mode_price)

            if bmx_diff_base_to_mode is None or bmx_diff_mode_to_base is None:
                logger.warning("Не удалось рассчитать арбитражные данные.")
                await asyncio.sleep(15)
                continue

            # Обновляем глобальные данные
            last_arbitrage_result["base_to_mode"] = bmx_diff_base_to_mode
            last_arbitrage_result["mode_to_base"] = bmx_diff_mode_to_base

            logger.info(f"BASE → MODE: {bmx_diff_base_to_mode:.2f}, MODE → BASE: {bmx_diff_mode_to_base:.2f}")

            # Алёрт по условию
            for user_id in allowed_users:
                if bmx_diff_base_to_mode > 0.5:
                    await application.bot.send_message(chat_id=user_id, text=f"Алёрт! BASE → MODE: {bmx_diff_base_to_mode:.2f} BMX.")
                if bmx_diff_mode_to_base > 0.5:
                    await application.bot.send_message(chat_id=user_id, text=f"Алёрт! MODE → BASE: {bmx_diff_mode_to_base:.2f} BMX.")

            await asyncio.sleep(15)

        except httpcore.ConnectTimeout:
            logger.error("Ошибка: таймаут подключения. Повтор через 15 секунд.")
            await asyncio.sleep(15)
        except Exception as e:
            logger.error(f"Непредвиденная ошибка: {e}")
            await asyncio.sleep(15)


# Функция ежечасного алёрта
async def hourly_alert():
    global last_arbitrage_result

    while True:
        try:
            current_time = datetime.now()

            # Проверяем начало нового часа
            if current_time.minute == 0:
                base_to_mode = last_arbitrage_result["base_to_mode"]
                mode_to_base = last_arbitrage_result["mode_to_base"]

                if base_to_mode is None or mode_to_base is None:
                    logger.warning("Ежечасный алерт: данные недоступны.")
                    await asyncio.sleep(60)
                    continue
                else:
                    for user_id in allowed_users:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"Бот в порядке. Ежечасный алёрт:\n"
                                f"BASE → MODE: {base_to_mode:.2f} BMX\n"
                                f"MODE → BASE: {mode_to_base:.2f} BMX"
                            )
                        )
                    logger.info("Ежечасный алёрт успешно отправлен.")

                # Спим до следующей минуты, чтобы не отправить алерт несколько раз в одном часу
                seconds_until_next_minute = 60 - current_time.second
                await asyncio.sleep(seconds_until_next_minute)
                continue

            # Спим до следующей проверки
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Ошибка в hourly_alert: {e}")
            await asyncio.sleep(60)


def get_latest_update():
    """Reads the latest update entry from the update history file."""
    try:
        with open("update_history.json", "r") as file:
            updates = json.load(file)
            if updates:
                latest_update = updates[-1]
                return {
                    "version": latest_update["version"],
                    "date": latest_update["date"],
                    "changes": latest_update["changes"]
                }
            else:
                return {
                    "version": "N/A",
                    "date": "N/A",
                    "changes": "No updates found in the history file."
                }
    except Exception as e:
        logger.error(f"Error retrieving update information: {e}")
        return {
            "version": "N/A",
            "date": "N/A",
            "changes": f"Error: {e}"
        }


async def notify_users_on_restart():
    global price_check_task, hourly_alert_task

    # Get task status
    tasks_status = []
    if price_check_task and not price_check_task.done():
        tasks_status.append("Task: Arbitrage price checking is running.")
    else:
        tasks_status.append("Task: Arbitrage price checking is NOT running.")

    if hourly_alert_task and not hourly_alert_task.done():
        tasks_status.append("Task: Hourly alert is running.")
    else:
        tasks_status.append("Task: Hourly alert is NOT running.")

    tasks_status_message = "\n".join(tasks_status)

    # Combine messages
    latest_update = get_latest_update()
    restart_message = (
        f"*Bot restarted with the latest update:*\n"
        f"Version: `{latest_update['version']}`\n"
        f"Date: `{latest_update['date']}`\n"
        f"Changes: `{latest_update['changes']}`\n\n"
        f"*Current task status:*\n"
        f"Task: `Arbitrage price checking is running.`\n"
        f"Task: `Hourly alert is running.`"
    )


    # Notify all allowed users
    for user_id in allowed_users:
        try:
            await application.bot.send_message(chat_id=user_id, text=restart_message, parse_mode="Markdown")
            logger.info(f"User {user_id} notified about restart.")
        except Exception as e:
            logger.error(f"Failed to notify user {user_id}: {e}")


async def on_startup(app: ContextTypes.DEFAULT_TYPE):
    """Start tasks and notify users about bot restart."""
    global price_check_task, hourly_alert_task

    # Start tasks if not already running
    if price_check_task is None or price_check_task.done():
        price_check_task = asyncio.create_task(check_prices_and_notify())
    if hourly_alert_task is None or hourly_alert_task.done():
        hourly_alert_task = asyncio.create_task(hourly_alert())

    # Notify users
    await notify_users_on_restart()


def restricted(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.message.chat_id
        if user_id not in allowed_users:
            await update.message.reply_text("У вас нет доступа к этому боту.")
            logger.warning(f"Пользователь {user_id} попытался использовать бота без разрешения.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global price_check_task, hourly_alert_task

    tasks_status = []
    if price_check_task and not price_check_task.done():
        tasks_status.append("Задача проверки арбитража запущена.")
    else:
        tasks_status.append("Задача проверки арбитража не активна.")

    if hourly_alert_task and not hourly_alert_task.done():
        tasks_status.append("Задача ежечасного алёрта запущена.")
    else:
        tasks_status.append("Задача ежечасного алёрта не активна.")

    # Отправка статуса задач
    tasks_status_message = "\n".join(tasks_status)
    await update.message.reply_text(f"Статус задач:\n{tasks_status_message}")


@restricted
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_arbitrage_result

    user_id = update.message.chat_id

    base_to_mode = last_arbitrage_result["base_to_mode"]
    mode_to_base = last_arbitrage_result["mode_to_base"]

    if base_to_mode is None or mode_to_base is None:
        logger.warning(f"user_id {user_id} нажал /price, но арбитражные данные недоступны. Либо он ещё не нажимал /start, либо бот поломался")
        await update.message.reply_text("Актуальные данные недоступны. Попробуй сперва /start, а потом уже /price. Если не поможет — значит, бот поломался :(")
    else:
        logger.info(f"user_id {user_id} нажал /price и получил актуальные данные")
        await update.message.reply_text(
            f"BASE → MODE: {base_to_mode:.2f} BMX\n"
            f"MODE → BASE: {mode_to_base:.2f} BMX"
        )


# Основной блок запуска
if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('price', price))

    # Используем post_init для инициализации задач
    application.post_init = on_startup

    # Запуск polling
    application.run_polling()
