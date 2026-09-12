"""
Persistence layer. Everything lives in one JSON file so a redeploy on Render
doesn't wipe your watches. If you want it to survive Render's ephemeral disk
across deploys, mount a Render Disk at /data and point DATA_PATH there.
"""
import json
import os
import threading
from datetime import date
from typing import Optional

DATA_PATH = os.environ.get("DATA_PATH", "watches.json")
_lock = threading.Lock()


def _empty_db():
    return {"watches": {}, "next_id": 1}


def load() -> dict:
    if not os.path.exists(DATA_PATH):
        return _empty_db()
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return _empty_db()


def save(db: dict) -> None:
    with _lock:
        tmp_path = DATA_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, DATA_PATH)


def create_watch(chat_id: int, city: str, date_start: str, date_end: str,
                  max_price: float) -> dict:
    db = load()
    watch_id = str(db["next_id"])
    db["next_id"] += 1
    watch = {
        "id": watch_id,
        "chat_id": chat_id,
        "city": city,
        "date_start": date_start,   # ISO "YYYY-MM-DD"
        "date_end": date_end,       # ISO "YYYY-MM-DD"
        "max_price": max_price,
        "hotel_filter": [],         # list of {"name":..., "url":...} — empty = all hotels
        "destination_params": None,  # filled in once we resolve the city on marriott.com
        "alerted": [],              # list of "hotel_key|YYYY-MM-DD" already sent
        "active": True,
    }
    db["watches"][watch_id] = watch
    save(db)
    return watch


def get_watch(watch_id: str) -> Optional[dict]:
    return load()["watches"].get(str(watch_id))


def update_watch(watch_id: str, **fields) -> None:
    db = load()
    if str(watch_id) in db["watches"]:
        db["watches"][str(watch_id)].update(fields)
        save(db)


def add_hotel_filter(watch_id: str, name: str, url: str) -> None:
    db = load()
    w = db["watches"].get(str(watch_id))
    if w:
        w["hotel_filter"].append({"name": name, "url": url})
        save(db)


def mark_alerted(watch_id: str, hotel_key: str, night: str) -> None:
    db = load()
    w = db["watches"].get(str(watch_id))
    if w:
        w["alerted"].append(f"{hotel_key}|{night}")
        save(db)


def already_alerted(watch: dict, hotel_key: str, night: str) -> bool:
    return f"{hotel_key}|{night}" in watch.get("alerted", [])


def list_active_watches(chat_id: Optional[int] = None) -> list:
    db = load()
    out = [w for w in db["watches"].values() if w["active"]]
    if chat_id is not None:
        out = [w for w in out if w["chat_id"] == chat_id]
    return out


def stop_watch(watch_id: str) -> bool:
    db = load()
    w = db["watches"].get(str(watch_id))
    if not w:
        return False
    w["active"] = False
    save(db)
    return True


def last_watch_for_chat(chat_id: int) -> Optional[dict]:
    watches = list_active_watches(chat_id)
    return watches[-1] if watches else None
