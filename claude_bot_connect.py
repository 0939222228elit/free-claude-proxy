"""
Claude Opus 5.5 Bot Connection Module & Configuration Kit
=========================================================
Универсальный файл подключения к AI-серверу Claude Opus 5.5 для чат-ботов
(Telegram, Discord, VK, WhatsApp, Web-виджеты и др.)

Все параметры и ключи уже настроены и готовы к работе.
Работает "из коробки" на стандартной библиотеке Python (без обязательных pip install),
а также поддерживает openai, anthropic, aiohttp, httpx.

Быстрый запуск для теста:
    python claude_bot_connect.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

# ==============================================================================
# 🔑 РЕКВИЗИТЫ ДОСТУПА И КОНФИГУРАЦИЯ (ВСЯ ИНФОРМАЦИЯ О КЛЮЧЕ)
# ==============================================================================
CLAUDE_CONFIG = {
    # Основной рабочий ключ доступа
    "API_KEY": "sk-fcc-master-key-2026",
    
    # Резервный мастер-пароль
    "BACKUP_KEY": "Elitkamen88",
    
    # Базовый URL для Anthropic API (/v1/messages)
    "ANTHROPIC_BASE_URL": "https://free-claude-code-proxy-kwbo.onrender.com",
    
    # Базовый URL для OpenAI-совместимых клиентов (/v1/chat/completions)
    "OPENAI_BASE_URL": "https://free-claude-code-proxy-kwbo.onrender.com/v1",
    
    # Основная модель интеллекта
    "PRIMARY_MODEL": "claude-opus-5.5",
    
    # Дополнительные поддерживаемые модели
    "FALLBACK_MODELS": [
        "claude-fable-5.5",
        "claude-3-7-sonnet-20250219",
        "claude-3-5-sonnet-20241022",
        "groq/qwen/qwen3.8-27b",
        "gemini/gemini-3.5-flash",
    ],
    
    # Веб-студия для мониторинга и управления
    "STUDIO_URL": "https://free-claude-code-proxy-kwbo.onrender.com/coder",
}

# Подгрузка из переменных окружения при наличии переопределений
API_KEY = os.getenv("CLAUDE_API_KEY", CLAUDE_CONFIG["API_KEY"])
BASE_URL = os.getenv("CLAUDE_BASE_URL", CLAUDE_CONFIG["ANTHROPIC_BASE_URL"])
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", CLAUDE_CONFIG["OPENAI_BASE_URL"])
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", CLAUDE_CONFIG["PRIMARY_MODEL"])


# ==============================================================================
# 🚀 1. ГОТОВЫЙ КЛИЕНТ НА СТАНДАРТНОЙ БИБЛИОТЕКЕ PYTHON (БЕЗ PIP INSTALL!)
# ==============================================================================
def ask_claude(
    prompt: str,
    system: str = "Ты — полезный, умный и дружелюбный ассистент чат-бота.",
    history: list = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    """
    Отправить сообщение в Claude Opus 5.5 и получить текстовый ответ.
    Работает в ЛЮБОМ Python 3 без установки сторонних библиотек.
    
    :param prompt: Сообщение пользователя
    :param system: Системный промпт (инструкция роли для бота)
    :param history: Предыдущие сообщения в формате [{"role": "user"|"assistant", "content": "..."}]
    :param model: Имя модели (по умолчанию claude-opus-5.5)
    :param max_tokens: Максимальное количество токенов в ответе
    :return: Текст ответа от модели
    """
    url = f"{BASE_URL.rstrip('/')}/v1/messages"
    
    messages = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": messages,
    }
    
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "user-agent": "claude-bot-connect/1.0",
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
            content_blocks = data.get("content", [])
            reply_text = "".join(
                block.get("text", "") for block in content_blocks if block.get("type") == "text"
            )
            return reply_text.strip()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Claude API Error [{e.code}]: {err_body}") from e
    except Exception as e:
        raise RuntimeError(f"Claude Connection Error: {str(e)}") from e


# ==============================================================================
# ⚡ 2. АСИНХРОННЫЙ КЛИЕНТ ДЛЯ ВЫСОКОНАГРУЖЕННЫХ БОТОВ (AIOGRAM / DISCORD)
# ==============================================================================
async def ask_claude_async(
    prompt: str,
    system: str = "Ты — полезный, умный и дружелюбный ассистент чат-бота.",
    history: list = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    """
    Асинхронный вызов для использования внутри асинхронных фреймворков (aiogram 3, discord.py, FastAPI).
    Если установлен aiohttp — использует его, иначе запускает ask_claude в пуле потоков.
    """
    import asyncio
    
    try:
        import aiohttp
        url = f"{BASE_URL.rstrip('/')}/v1/messages"
        messages = []
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": messages,
        }
        headers = {
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=45)) as resp:
                data = await resp.json()
                content_blocks = data.get("content", [])
                return "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text").strip()
    except ImportError:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, ask_claude, prompt, system, history, model, max_tokens, temperature
        )


# ==============================================================================
# 🤖 3. ПРИМЕР ПОДКЛЮЧЕНИЯ К TELEGRAM БОТУ (AIOGRAM 3)
# ==============================================================================
TELEGRAM_AIOGRAM_EXAMPLE = '''
# pip install aiogram
import asyncio
from aiogram import Bot, Dispatcher, types
from claude_bot_connect import ask_claude_async

BOT_TOKEN = "ВАШ_ТОКЕН_ТЕЛЕГРАМ_БОТА_ИЗ_BOTFATHER"
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message()
async def handle_message(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        reply = await ask_claude_async(
            prompt=message.text,
            system="Ты вежливый помощник в Telegram. Отвечай кратко и емко."
        )
        await message.answer(reply)
    except Exception as e:
        await message.answer(f"Ошибка ИИ: {e}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
'''

# ==============================================================================
# 📦 4. ПРИМЕР С ОФИЦИАЛЬНЫМИ БИБЛИОТЕКАМИ OPENAI / ANTHROPIC (ЕСЛИ ИСПОЛЬЗУЕТЕ ИХ)
# ==============================================================================
SDK_EXAMPLES = '''
# Вариант А: Официальный Anthropic SDK (pip install anthropic)
import anthropic
client = anthropic.Anthropic(
    api_key="sk-fcc-master-key-2026",
    base_url="https://free-claude-code-proxy-kwbo.onrender.com"
)
resp = client.messages.create(
    model="claude-opus-5.5",
    max_tokens=1000,
    messages=[{"role": "user", "content": "Привет!"}]
)
print(resp.content[0].text)

# Вариант Б: Официальный OpenAI SDK (pip install openai)
from openai import OpenAI
client = OpenAI(
    api_key="sk-fcc-master-key-2026",
    base_url="https://free-claude-code-proxy-kwbo.onrender.com/v1"
)
resp = client.chat.completions.create(
    model="claude-opus-5.5",
    messages=[{"role": "user", "content": "Привет!"}]
)
print(resp.choices[0].message.content)
'''


# ==============================================================================
# 🛠️ 5. САМОТЕСТИРОВАНИЕ И ПРОВЕРКА ПОДКЛЮЧЕНИЯ ПРИ ПРЯМОМ ЗАПУСКЕ
# ==============================================================================
if __name__ == "__main__":
    print("=" * 70)
    print("🤖 CLAUDE OPUS 5.5 — ТЕСТ ПОДКЛЮЧЕНИЯ БОТА")
    print("=" * 70)
    print(f"• URL Прокси:   {CLAUDE_CONFIG['ANTHROPIC_BASE_URL']}")
    print(f"• API Ключ:     {CLAUDE_CONFIG['API_KEY'][:12]}...{CLAUDE_CONFIG['API_KEY'][-4:]}")
    print(f"• Модель:       {CLAUDE_CONFIG['PRIMARY_MODEL']}")
    print("-" * 70)
    print("📡 Отправка тестового запроса к серверу...")
    
    try:
        test_prompt = "Напиши ровно одно приветственное предложение от имени Claude Opus 5.5 для чат-бота."
        response = ask_claude(test_prompt)
        print("\n✅ УСПЕШНОЕ ПОДКЛЮЧЕНИЕ!")
        print(f"Ответ модели: \"{response}\"")
        print("\n🎉 Ваш файл подключения полностью готов к работе в любом чат-боте!")
    except Exception as exc:
        print(f"\n❌ Ошибка проверки связи: {exc}")
        sys.exit(1)
