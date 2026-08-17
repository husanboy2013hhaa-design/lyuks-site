import json
import logging
import os
import threading
from pathlib import Path

import telebot
from dotenv import load_dotenv
from flask import Flask, jsonify, request

# Without this, a polling failure prints "polling exited" and nothing about
# why — while Flask keeps serving, so the bot looks healthy but answers
# nobody. See the ConnectionError/409 handling at the bottom.
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("lyuks")

HERE = Path(__file__).resolve().parent

# Always the .env sitting next to this file, whatever directory you launch
# python from — otherwise a stray .env one level up silently wins.
load_dotenv(HERE / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL")

# ADMIN_ID may hold one id, or several separated by commas: "123,456".
# We turn it into a set of ints so we can check `chat.id in ADMIN_IDS`.
ADMIN_IDS = {
    int(x) for x in (os.getenv("ADMIN_ID") or "").replace(" ", "").split(",") if x
}

# Render gives us this address automatically once the service is live.
# On your laptop it is empty -> the bot falls back to polling.
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")

# The website's address, so the browser is allowed to POST orders here.
# "*" while testing; set it to your real domain in production.
SITE_ORIGIN = os.getenv("SITE_ORIGIN", "*")

# Our own copy of the price list. Orders arrive from a browser, where every
# number can be edited before sending, so we price them from this instead.
# Generated from the site's src/data/products.js — after changing a price
# there, run `npm run gen-products` in the site folder to refresh this file,
# or the bot keeps charging the old price.
CATALOG = json.loads((HERE / "products.json").read_text(encoding="utf-8"))

# --- checks so mistakes are easy to read ---------------------------------
if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN .env faylda yo'q!")

if not WEBAPP_URL or not WEBAPP_URL.startswith("https://"):
    raise SystemExit(
        "❌ WEBAPP_URL .env faylda to'ldirilmagan!\n"
        "   Masalan: WEBAPP_URL=https://mini-app-five-indol.vercel.app"
    )

if not ADMIN_IDS:
    print("⚠️  ADMIN_ID .env faylda yo'q — buyurtmalar sizga yuborilmaydi!\n")

# "*" is fine on a laptop, but in production it lets any site on the internet
# POST orders into your admin chat.
if SITE_ORIGIN == "*" and WEBHOOK_URL:
    print(
        "⚠️  SITE_ORIGIN qo'yilmagan — /order har qanday saytga ochiq!\n"
        "   Render'da SITE_ORIGIN=https://sizning-saytingiz.vercel.app qiling.\n"
    )

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)


# ---------------------------------------------------------------- /start
@bot.message_handler(commands=["start"])
def ilova(message):
    bot.send_message(
        message.chat.id,
        f"Assalomu alaykum, {message.from_user.first_name}! 👋\n\n"
        "Lyuks Do'konga xush kelibsiz.\n"
        f"Do'konimiz shu yerda: {WEBAPP_URL}",
    )


# ------------------------------------------------------------------ /id
@bot.message_handler(commands=["id"])
def my_id(message):
    """Tells you your own chat id, so you can add yourself to ADMIN_ID."""
    uid = message.from_user.id
    known = "✅ Siz adminsiz" if uid in ADMIN_IDS else "❌ Siz admin emassiz"
    bot.send_message(
        message.chat.id,
        f"🆔 Sizning ID: `{uid}`\n\n{known}\n\n"
        "Admin bo'lish uchun .env faylga qo'shing:\n"
        f"`ADMIN_ID={','.join(str(a) for a in sorted(ADMIN_IDS | {uid}))}`\n"
        "so'ng botni qayta ishga tushiring.",
        parse_mode="Markdown",
    )


# ------------------------------------------------- order text formatting
def order_text(order):
    """One layout for every order, whatever route it arrived by."""
    text = "🛒 Yangi buyurtma!\n\n"
    for item in order["items"]:
        line_total = item["price"] * item["qty"]
        text += f"• {item['name']} × {item['qty']} = {line_total:,} so'm\n"
    text += f"\n💰 Jami: {order['total']:,} so'm\n\n"
    c = order["customer"]
    text += f"👤 {c.get('name', '—')}\n"
    text += f"📞 {c.get('phone', '—')}\n"
    text += f"📍 {c.get('address', '—')}\n"
    if c.get("comment"):
        text += f"💬 {c['comment']}\n"
    return text


def notify_admins(text):
    """Deliver to every admin. One bad chat must not silence the others —
    an admin who blocked the bot or never pressed /start raises here."""
    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text)
            delivered += 1
        except Exception as e:
            print(f"⚠️  admin {admin_id} ga yuborilmadi: {e}")
    return delivered


# ------------------------------------------------- order from the website
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = SITE_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


def max_qty(stock):
    """Most units of one product an order may contain.

    Must match maxQty() in the site's CartContext.jsx. Some stock is weighed,
    so it arrives fractional (0.506) — plain int() would round that to 0 and
    silently drop a line the customer was allowed to add.
    """
    if stock <= 0:
        return 0
    return max(1, int(stock))


def price_order(raw):
    """Rebuild the order from our own catalogue.

    Everything in `raw` came out of a browser, where the customer can edit
    it first — so names, prices and the total are all re-derived here from
    the product id. Only the id and the quantity are taken on trust, and
    the quantity is capped at what we actually have in stock.
    """
    items, total = [], 0
    for line in raw.get("items", []):
        product = CATALOG.get(str(line.get("id")))
        if not product:
            continue  # unknown id — drop the line rather than guess
        try:
            qty = int(line.get("qty", 0))
        except (TypeError, ValueError):
            continue
        qty = max(0, min(qty, max_qty(product["stock"])))
        if qty == 0:
            continue
        items.append({"name": product["name"], "qty": qty, "price": product["price"]})
        total += product["price"] * qty
    return {"customer": raw.get("customer", {}), "items": items, "total": total}


@app.route("/order", methods=["POST", "OPTIONS"])
def site_order():
    if request.method == "OPTIONS":  # browser preflight
        return _cors(app.make_default_options_response())

    raw = request.get_json(silent=True) or {}
    customer = raw.get("customer") or {}
    if not customer.get("name") or not customer.get("phone"):
        return _cors(jsonify(ok=False, error="ism va telefon kerak")), 400

    order = price_order(raw)
    if not order["items"]:
        return _cors(jsonify(ok=False, error="savat bo'sh")), 400

    # Flag it when the browser's total disagreed with ours — a mismatch is
    # either a stale price list or someone editing numbers before sending.
    note = ""
    try:
        if int(raw.get("total", -1)) != order["total"]:
            note = f"\n⚠️  Brauzer {int(raw.get('total', 0)):,} so'm ko'rsatgan edi"
    except (TypeError, ValueError):
        pass

    # Tagged so a site order is never mistaken for one placed in Telegram:
    # a web customer has no Telegram id, so you must phone them back.
    text = "🌐 SAYTDAN buyurtma\n\n" + order_text(order) + note

    if notify_admins(text) == 0:
        return _cors(jsonify(ok=False, error="admin topilmadi")), 502

    return _cors(jsonify(ok=True))


# ------------------------------------------------------- web (Render)
@app.route("/", methods=["GET"])
def health():
    """Render pings this to see the service is alive."""
    return "Bot ishlayapti ✅", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    """Telegram knocks here every time someone messages the bot."""
    raw = request.stream.read().decode("utf-8")
    update = telebot.types.Update.de_json(raw)
    bot.process_new_updates([update])
    return "", 200


# Register the webhook as soon as the app starts on Render.
if WEBHOOK_URL:
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    print(f"Webhook o'rnatildi: {WEBHOOK_URL}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))

    if WEBHOOK_URL:
        # Running on a server (webhook mode)
        app.run(host="0.0.0.0", port=port)
    else:
        # Running on your laptop. The site still needs /order to answer,
        # so Flask goes in a background thread while we poll on the main
        # one. use_reloader=False is required — the reloader only works
        # from the main thread.
        threading.Thread(
            target=lambda: app.run(
                host="127.0.0.1", port=port, use_reloader=False
            ),
            daemon=True,
        ).start()

        me = bot.get_me()
        log.info("Bot: @%s (id %s)", me.username, me.id)
        log.info("Adminlar: %s", sorted(ADMIN_IDS) or "YO'Q — /id yuboring")
        log.info("Local rejim: polling + http://127.0.0.1:%s/order", port)

        bot.remove_webhook()
        try:
            # skip_pending drops messages sent while the bot was off, so a
            # restart doesn't replay an old backlog.
            bot.infinity_polling(skip_pending=True, timeout=30)
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                # The usual cause of "the bot stopped answering": a second
                # copy is polling the same token. Telegram allows only one.
                raise SystemExit(
                    "\n❌ 409 Conflict — bu bot boshqa joyda ham ishlayapti!\n"
                    "   Faqat bitta nusxa ishlashi kerak.\n"
                    "   Tekshiring:  Get-Process python\n"
                    "   To'xtating:   Stop-Process -Name python\n"
                )
            raise
        # infinity_polling swallows its own errors and returns. Without this
        # the process would sit here with Flask still serving /order, looking
        # healthy while answering nobody in Telegram.
        raise SystemExit(
            "\n❌ Polling to'xtadi — bot endi Telegramda javob bermaydi.\n"
            "   Yuqoridagi xatoni o'qing va botni qayta ishga tushiring.\n"
        )