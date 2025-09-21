# main_web_v2.py
# -----------------------------
# 1) Универсальный вебхук Telegram: /webhook и /webhook/<secret>
# 2) Health-check: /healthz
# 3) Авто-установка вебхука при старте (если задан WEBHOOK_URL)
# 4) Фильтры ALLOWED_CHAT_ID / ALLOWED_TOPIC_ID
# 5) Мини-обработчик входящих сообщений/документов (для проверки трафика)
# -----------------------------

import os
import json
import logging
from typing import Any, Dict, Optional

from flask import Flask, request, abort, jsonify
from urllib.parse import urlencode
import urllib.request

# ---------- ЛОГИ ----------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("tg-webhook")

# ---------- ENV ----------
def get_env() -> Dict[str, str]:
    # Поддерживаем и BOT_TOKEN, и TELEGRAM_BOT_TOKEN (чтобы не менять Render)
    bot_token = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    webhook_url = (os.getenv("WEBHOOK_URL") or "").strip()
    webhook_secret = (os.getenv("WEBHOOK_SECRET") or "").strip()

    allowed_chat_id = (os.getenv("ALLOWED_CHAT_ID") or "").strip()
    allowed_topic_id = (os.getenv("ALLOWED_TOPIC_ID") or "").strip()

    if not bot_token:
        raise RuntimeError("Не задан BOT_TOKEN (или TELEGRAM_BOT_TOKEN) в переменных окружения.")

    # WEBHOOK_URL НЕ обязателен для старта сервера — но без него авто-настройка вебхука не выполнится.
    return {
        "BOT_TOKEN": bot_token,
        "WEBHOOK_URL": webhook_url,
        "WEBHOOK_SECRET": webhook_secret,
        "ALLOWED_CHAT_ID": allowed_chat_id,
        "ALLOWED_TOPIC_ID": allowed_topic_id,
    }

ENV = get_env()

# ---------- UTILS ----------
def tg_api_request(method: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Вызов Telegram Bot API без внешних библиотек.
    """
    url = f"https://api.telegram.org/bot{ENV['BOT_TOKEN']}/{method}"
    body = urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            log.error("TG API invalid JSON: %s", raw)
            return {"ok": False, "raw": raw}

def set_webhook_if_needed() -> None:
    """
    Если задан WEBHOOK_URL — пытаемся зарегистрировать вебхук в Telegram.
    """
    if not ENV["WEBHOOK_URL"]:
        log.info("WEBHOOK_URL не задан — пропускаю установку вебхука (сервер всё равно поднят).")
        return

    # Если используем secret-путь, проверим, что URL действительно содержит его.
    if ENV["WEBHOOK_SECRET"]:
        if not ENV["WEBHOOK_URL"].rstrip("/").endswith(f"/{ENV['WEBHOOK_SECRET']}"):
            log.warning(
                "WEBHOOK_SECRET задан, но WEBHOOK_URL не заканчивается на '/%s'. "
                "Рекомендуется сделать URL вида .../webhook/%s",
                ENV["WEBHOOK_SECRET"], ENV["WEBHOOK_SECRET"]
            )

    log.info("Настраиваю вебхук Telegram: %s", ENV["WEBHOOK_URL"])
    res = tg_api_request("setWebhook", {"url": ENV["WEBHOOK_URL"]})
    if not res.get("ok"):
        log.error("Не удалось установить вебхук: %s", res)
    else:
        log.info("Вебхук установлен: %s", res)

def send_text(chat_id: int, text: str, message_thread_id: Optional[int] = None) -> None:
    payload = {"chat_id": chat_id, "text": text}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    tg_api_request("sendMessage", payload)

def passes_filters(msg: Dict[str, Any]) -> bool:
    """
    Фильтруем по ALLOWED_CHAT_ID / ALLOWED_TOPIC_ID, если заданы.
    """
    if not msg:
        return False

    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")

    # Для тем (топиков) в супергруппах
    topic_id = msg.get("message_thread_id")
    topic_id_str = str(topic_id) if topic_id is not None else ""

    allowed_chat_id = ENV["ALLOWED_CHAT_ID"]
    allowed_topic_id = ENV["ALLOWED_TOPIC_ID"]

    if allowed_chat_id and chat_id != allowed_chat_id:
        return False
    if allowed_topic_id:
        # Если фильтруем по теме — требуем совпадение ID темы
        if topic_id_str != allowed_topic_id:
            return False
    return True

def handle_update(update: Dict[str, Any]) -> None:
    """
    Мини-обработчик: только логируем и отправляем короткий ответ,
    чтобы проверить сквозную связку Telegram → Render → Telegram.
    """
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        log.info("Нет поля message в апдейте — пропускаю.")
        return

    if not passes_filters(message):
        log.info("Сообщение отфильтровано по ALLOWED_*")
        return

    chat = message["chat"]
    chat_id = int(chat["id"])
    thread_id = message.get("message_thread_id")  # для тем

    # Определяем тип входящего
    if "document" in message:
        doc = message["document"]
        file_name = doc.get("file_name", "document")
        log.info("Получен document: %s", file_name)
        send_text(chat_id, f"✅ Файл получен: {file_name}. Обработка включена.", thread_id)
        return
    elif "text" in message:
        txt = message["text"]
        log.info("Получен текст: %s", txt)
        send_text(chat_id, "👋 Бот на связи. Отправь PDF/DOCX как файл (скрепкой).", thread_id)
        return
    else:
        log.info("Получен апдейт не поддерживаемого типа: ключи=%s", list(message.keys()))
        send_text(chat_id, "Я пока принимаю текст и документы (PDF/DOCX).", thread_id)
        return

# ---------- APP ----------
app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "ok", 200

# Принимаем и /webhook, и /webhook/<secret>
@app.route("/webhook", methods=["GET", "POST"])
@app.route("/webhook/<secret>", methods=["GET", "POST"])
def webhook(secret: Optional[str] = None):
    # Если задан секрет — проверяем
    if ENV["WEBHOOK_SECRET"]:
        if secret is None or secret != ENV["WEBHOOK_SECRET"]:
            # GET/POST по "неправильному" URL — 403, чтобы было видно, что маршрут живой, но секрет не тот.
            return "forbidden", 403

    if request.method == "GET":
        # Telegram использует POST, но GET держим для быстрой проверки в браузере.
        return "ok", 200

    # POST — это от Telegram
    try:
        update = request.get_json(force=True, silent=True) or {}
    except Exception:
        log.exception("Не удалось распарсить JSON")
        return jsonify({"ok": False, "error": "invalid json"}), 400

    log.info("Incoming update: %s", json.dumps(update)[:2000])
    try:
        handle_update(update)
    except Exception:
        log.exception("Ошибка в обработке апдейта")
        return jsonify({"ok": False}), 500

    return jsonify({"ok": True}), 200

def main():
    # Пытаемся выставить вебхук (если задан WEBHOOK_URL)
    set_webhook_if_needed()

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()

