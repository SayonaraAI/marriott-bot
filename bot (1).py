"""
Marriott associate-rate watcher — Telegram bot.

Commands
  /watch <ville> <j1-j2/mois> <prix_max>
      ex: /watch madrid 1-10/10 250€
      Crée une watch sur Madrid, nuits du 1 au 10 octobre, sous 250€.
      Sans lien d'hôtel envoyé ensuite -> alerte sur n'importe quel hôtel.

  (message texte contenant un lien marriott.com/.../hotels/...)
      Ajoute cet hôtel à la watch la plus récente comme filtre. Une fois
      qu'au moins un hôtel est ajouté, seuls ces hôtels-là déclenchent une
      alerte pour cette watch.

  /list        Liste tes watches actives.
  /stop <id>   Arrête une watch.
  /help        Rappel des commandes.
"""
import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import (Application, CommandHandler, ContextTypes,
                           MessageHandler, filters)

import scraper
import storage

load_dotenv()
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", "60"))
# Marriott almost certainly rate-limits/blocks aggressive scraping (Akamai or
# similar is common on big hotel sites). 60s across many nights x many
# watches can look like a bot fast. Consider raising this if you start
# getting blocked, or if a captcha shows up in the scraped pages.
DELAY_BETWEEN_NIGHTS_SECONDS = float(os.environ.get("DELAY_BETWEEN_NIGHTS_SECONDS", "3"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("marriott-bot")

HOTEL_LINK_RE = re.compile(r"https?://(?:www\.)?marriott\.com/\S+")


def parse_watch_args(args: list) -> tuple[str, str, str, float]:
    """
    args like: ["madrid", "1-10/10", "250\u20ac"]
    Returns (city, date_start_iso, date_end_iso, max_price)
    """
    if len(args) < 3:
        raise ValueError(
            "Format attendu : /watch <ville> <j1-j2/mois> <prix_max>\n"
            "Exemple : /watch madrid 1-10/10 250"
        )
    city = args[0]
    date_part = args[1]
    price_part = args[2]

    m = re.match(r"^(\d{1,2})-(\d{1,2})/(\d{1,2})$", date_part)
    if not m:
        raise ValueError("Format de dates invalide. Exemple : 1-10/10 (du 1 au 10 octobre)")
    d1, d2, month = (int(x) for x in m.groups())

    today = date.today()
    year = today.year
    start = date(year, month, d1)
    end = date(year, month, d2)
    if start < today:
        # roll to next year if the range has already passed this year
        start = date(year + 1, month, d1)
        end = date(year + 1, month, d2)

    price_str = re.sub(r"[^\d.]", "", price_part)
    if not price_str:
        raise ValueError("Prix max invalide. Exemple : 250 ou 250\u20ac")
    max_price = float(price_str)

    return city, start.isoformat(), end.isoformat(), max_price


def nights_in_range(date_start: str, date_end: str) -> list:
    start = datetime.strptime(date_start, "%Y-%m-%d").date()
    end = datetime.strptime(date_end, "%Y-%m-%d").date()
    nights = []
    d = start
    while d < end:  # last night is checkin = end - 1
        nights.append(d.isoformat())
        d += timedelta(days=1)
    return nights


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        city, date_start, date_end, max_price = parse_watch_args(context.args)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    watch = storage.create_watch(update.effective_chat.id, city, date_start, date_end, max_price)
    await update.message.reply_text(
        f"\u2705 Watch #{watch['id']} cr\u00e9\u00e9e : {city.title()}, "
        f"du {date_start} au {date_end}, max {max_price:g}\u20ac.\n"
        f"Envoie-moi un lien marriott.com d'un h\u00f4tel pour restreindre "
        f"cette watch \u00e0 une liste pr\u00e9cise (sinon j'alerte sur "
        f"n'importe quel h\u00f4tel du secteur en dessous du prix)."
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    watches = storage.list_active_watches(update.effective_chat.id)
    if not watches:
        await update.message.reply_text("Aucune watch active.")
        return
    lines = []
    for w in watches:
        filt = ", ".join(h["name"] for h in w["hotel_filter"]) or "tous les h\u00f4tels"
        lines.append(
            f"#{w['id']} \u2014 {w['city'].title()} \u00b7 {w['date_start']} \u2192 {w['date_end']} "
            f"\u00b7 \u2264{w['max_price']:g}\u20ac \u00b7 [{filt}]"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /stop <id>")
        return
    ok = storage.stop_watch(context.args[0])
    await update.message.reply_text("\u2705 Watch arr\u00eat\u00e9e." if ok else "Watch introuvable.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/watch <ville> <j1-j2/mois> <prix_max> \u2014 ex: /watch madrid 1-10/10 250\n"
        "Envoie un lien marriott.com apr\u00e8s /watch pour restreindre \u00e0 des h\u00f4tels pr\u00e9cis\n"
        "/list \u2014 liste tes watches actives\n"
        "/stop <id> \u2014 arr\u00eate une watch"
    )


async def handle_hotel_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = HOTEL_LINK_RE.search(text)
    if not match:
        return
    watch = storage.last_watch_for_chat(update.effective_chat.id)
    if not watch:
        await update.message.reply_text("Cr\u00e9e d'abord une watch avec /watch.")
        return
    url = match.group(0)
    # best-effort name: property slug from the URL, refine later if needed
    m = re.search(r"/hotels/([a-zA-Z0-9\-]+)/", url + "/")
    name = m.group(1) if m else url
    storage.add_hotel_filter(watch["id"], name, url)
    await update.message.reply_text(f"\u2795 Ajout\u00e9 \u00e0 la watch #{watch['id']} : {name}")


async def scan_loop(app: Application):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            while True:
                try:
                    await scan_all_watches(app, browser)
                except Exception:
                    log.exception("Erreur pendant le scan")
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
        finally:
            await browser.close()


async def scan_all_watches(app: Application, browser):
    watches = storage.list_active_watches()
    if not watches:
        return

    # cache resolved destinations per city within this pass
    dest_cache = {}

    for watch in watches:
        city_key = watch["city"].lower()
        dest = watch.get("destination_params") or dest_cache.get(city_key)
        if not dest:
            try:
                dest = await scraper.resolve_destination(browser, watch["city"])
                dest_cache[city_key] = dest
                storage.update_watch(watch["id"], destination_params=dest)
            except Exception:
                log.exception(f"Impossible de r\u00e9soudre la ville '{watch['city']}'")
                continue

        allowed_names = {h["name"] for h in watch["hotel_filter"]}

        for night in nights_in_range(watch["date_start"], watch["date_end"]):
            try:
                hotels = await scraper.search_night(browser, dest, night)
            except Exception:
                log.exception(f"Erreur de scan pour {watch['city']} le {night}")
                await asyncio.sleep(DELAY_BETWEEN_NIGHTS_SECONDS)
                continue

            for hotel in hotels:
                if hotel.get("price") is None:
                    continue
                if hotel["price"] > watch["max_price"]:
                    continue
                if allowed_names and not any(
                    n.lower() in (hotel.get("name") or "").lower() for n in allowed_names
                ):
                    continue

                key = scraper.hotel_key(hotel)
                if storage.already_alerted(watch, key, night):
                    continue

                await app.bot.send_message(
                    chat_id=watch["chat_id"],
                    text=(
                        f"\U0001F3E8 {hotel['name']}\n"
                        f"\U0001F4C5 Nuit du {night}\n"
                        f"\U0001F4B0 {hotel['price']:g}\u20ac (code MMA)\n"
                        f"{hotel.get('link') or ''}"
                    ),
                )
                storage.mark_alerted(watch["id"], key, night)

            await asyncio.sleep(DELAY_BETWEEN_NIGHTS_SECONDS)


async def post_init(app: Application):
    app.create_task(scan_loop(app))


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_hotel_link))
    app.run_polling()


if __name__ == "__main__":
    main()
