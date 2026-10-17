"""
Синхронизация встроенной автовыдачи FunPay (ADSync) — плагин для FunPay Cardinal v0.1.17+

🔗 Держит ОДИНАКОВОЕ содержимое поля автовыдачи FunPay («секреты»/товары) у
нескольких лотов сразу — «группы синхронизации».

Зачем:
 • Один и тот же пул товаров продаётся с нескольких лотов (разные игры/подкатегории).
 • Купили на одном лоте → FunPay списала товар только с него → на остальных лотах
   товар остаётся и может продаться ВТОРОЙ раз. ADSync убирает проданный товар
   со всех лотов группы (сразу после заказа + периодической проверкой).
 • Пополнили товары на одном лоте (через сайт) → они появляются на всех лотах группы.

Как работает:
 • Группы синхронизации: у каждой свой список лотов и «базовый срез» товаров.
 • Алгоритм слияния: чего нет хотя бы у одного лота группы — считается проданным/
   удалённым и убирается у всех; что добавлено хотя бы на одном лоте — добавляется
   всем. Работает даже если за один интервал изменения были на нескольких лотах.
 • Триггеры: новый заказ по лоту группы (мгновенно), периодическая проверка
   (по умолчанию 60 сек.), ручная кнопка «Синхронизировать сейчас».
 • При добавлении лота в группу его товары сразу заменяются общим списком группы.
 • Уведомления только о реальных изменениях (без спама).

Команды Telegram:
 • /adsync — открыть меню плагина.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
Автор: yungspider
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

logger = logging.getLogger("FPC.ADSync")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "ADSync (FunPay Auto-Delivery Sync)"
VERSION = "1.0.0"
DESCRIPTION = ("Синхронизация встроенной автовыдачи FunPay на несколько лотов сразу: одинаковое "
               "содержимое поля товаров у всех лотов группы. Продажа на одном лоте убирает товар "
               "со всех остальных, пополнение расходится на все. Команда: /adsync")
CREDITS = "yungspider"
UUID = "1cb469ce-8804-4f2f-98ed-7c347a168ab9"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "ad_sync")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "groups": [],       # [{"id", "name", "enabled", "lots": [int], "baseline": [str]|None, "last": {...}}]
    "interval": 60,     # интервал периодической синхронизации, сек
}
INTERVAL_RANGE = (30, 900)
PER_PAGE = 10
READ_DELAY = 0.4    # пауза между чтениями полей лотов
SAVE_DELAY = 0.8    # пауза между сохранениями лотов (анти-флуд)

# ══════════════════════════════ CALLBACK-КОДЫ ═══════════════════════════════════
CB_HOME = "ads:home"
CB_GROUP = "ads:grp"        # ads:grp:{gid}
CB_GADD = "ads:gadd"
CB_GTOG = "ads:gtog"        # ads:gtog:{gid}
CB_GDEL = "ads:gdel"        # ads:gdel:{gid}
CB_GDELY = "ads:gdely"      # ads:gdely:{gid}
CB_GNAME = "ads:gname"      # ads:gname:{gid}
CB_GSYNC = "ads:gsync"      # ads:gsync:{gid}
CB_PICK = "ads:pick"        # ads:pick:{gid}:{offset}
CB_PTOG = "ads:ptog"        # ads:ptog:{gid}:{lot_id}:{offset}
CB_PREM = "ads:prem"        # ads:prem:{gid}:{lot_id}
CB_INT = "ads:int"
CB_NOOP = "ads:noop"
ST_GNAME = "ads:st:gname"
ST_INT = "ads:st:int"

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_sync_lock = threading.Lock()
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _normalize_group(g: dict, fallback_id: int) -> dict | None:
    if not isinstance(g, dict):
        return None
    try:
        gid = int(g.get("id", fallback_id))
    except (TypeError, ValueError):
        gid = fallback_id
    lots = []
    if isinstance(g.get("lots"), list):
        for lot in g["lots"]:
            try:
                lot = int(lot)
            except (TypeError, ValueError):
                continue
            if lot not in lots:
                lots.append(lot)
    baseline = g.get("baseline")
    if baseline is not None:
        if not isinstance(baseline, list):
            baseline = None
        else:
            baseline = [str(i) for i in baseline]
    last = g.get("last")
    if not isinstance(last, dict):
        last = {}
    return {"id": gid, "name": str(g.get("name") or f"Группа {gid}")[:60],
            "enabled": bool(g.get("enabled", True)), "lots": lots,
            "baseline": baseline, "last": last}


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("ADSync: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        groups = []
        if isinstance(settings["groups"], list):
            for i, g in enumerate(settings["groups"], start=1):
                ng = _normalize_group(g, i)
                if ng:
                    groups.append(ng)
        settings["groups"] = groups
        try:
            settings["interval"] = _clamp(int(settings["interval"]), *INTERVAL_RANGE)
        except (TypeError, ValueError):
            settings["interval"] = DEFAULT_SETTINGS["interval"]
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("ADSync: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _get_group(settings: dict, gid: int) -> dict | None:
    for g in settings["groups"]:
        if g["id"] == gid:
            return g
    return None


def _next_group_id(settings: dict) -> int:
    return max([g["id"] for g in settings["groups"]], default=0) + 1


def _group_of_lot(settings: dict, lot_id) -> dict | None:
    for g in settings["groups"]:
        if int(lot_id) in g["lots"]:
            return g
    return None


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def _notify(crd: "Cardinal", text: str) -> None:
    if not crd.telegram:
        return
    kwargs = {"notification_type": tg_utils.NotificationTypes.other} if tg_utils else {}
    try:
        crd.telegram.send_notification(text, **kwargs)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════════ СИНХРОНИЗАЦИЯ ═══════════════════════════════════
def _merge_secrets(baseline: list, current: dict) -> tuple:
    """
    Сливает базовый срез с текущими списками лотов.

    :param baseline: базовый срез товаров группы.
    :param current: {lot_id: [секреты]} — успешно прочитанные лоты.
    :return: (merged_list, removed_count, added_count)
    """
    base_set = set(baseline)
    removed = {p for p in base_set if any(p not in secrets for secrets in current.values())}
    added = []
    for secrets in current.values():
        for s in secrets:
            if s not in base_set and s not in added:
                added.append(s)
    merged = [p for p in baseline if p not in removed] + added
    return merged, len(removed), len(added)


def sync_group(crd: "Cardinal", group: dict) -> dict | None:
    """
    Один проход синхронизации группы. Изменяет group["baseline"]/["last"] на месте.
    Возвращает отчет {"changed": [lot_id...], "total": int, "removed": int, "added": int,
    "read": int, "no_ad": [lot_id...]} или None при ошибке.
    """
    with _sync_lock:
        current = {}
        no_ad = []
        fields_cache = {}
        for lot_id in group["lots"]:
            if _stop_event.is_set():
                return None
            try:
                fields = crd.account.get_lot_fields(int(lot_id))
            except Exception:
                logger.error(f"ADSync: не удалось прочитать поля лота {lot_id} "
                             f"(группа «{group['name']}»).")
                logger.debug("TRACEBACK", exc_info=True)
                continue
            fields_cache[lot_id] = fields
            current[lot_id] = list(getattr(fields, "secrets", []) or [])
            if not getattr(fields, "auto_delivery", False):
                no_ad.append(lot_id)
            time.sleep(READ_DELAY)
        if not current:
            return None

        baseline = group.get("baseline")
        if not isinstance(baseline, list):
            # первый проход: источник — первый доступный лот группы, без слияния
            # (иначе стартовые различия лотов алгоритм слияния
            #  посчитал бы продажами и затёр товары первого лота)
            first_id = next(lid for lid in group["lots"] if lid in current)
            merged = list(current[first_id])
            removed_n = added_n = 0
            logger.info(f"ADSync: группа «{group['name']}» — базовый срез взят с лота {first_id}.")
        else:
            merged, removed_n, added_n = _merge_secrets(baseline, current)

        changed = []
        for lot_id, fields in fields_cache.items():
            if list(getattr(fields, "secrets", []) or []) == merged:
                continue
            try:
                fields.secrets = list(merged)
                crd.account.save_lot(fields)
                changed.append(lot_id)
                logger.info(f"ADSync: лот {lot_id} (группа «{group['name']}») "
                            f"синхронизирован → {len(merged)} товаров.")
                time.sleep(SAVE_DELAY)
            except Exception:
                logger.error(f"ADSync: не удалось сохранить лот {lot_id} (группа «{group['name']}»).")
                logger.debug("TRACEBACK", exc_info=True)

        group["baseline"] = merged
        group["last"] = {"ts": time.time(), "changed": len(changed), "total": len(merged),
                         "no_ad": no_ad}
        return {"changed": changed, "total": len(merged), "removed": removed_n,
                "added": added_n, "read": len(current), "no_ad": no_ad}


def _sync_all(crd: "Cardinal") -> None:
    """Периодический проход по всем включенным группам + сохранение настроек."""
    settings = _load_settings()
    dirty = False
    for group in settings["groups"]:
        if not group["enabled"] or not group["lots"]:
            continue
        report = sync_group(crd, group)
        if report is None:
            continue
        dirty = True
        if report["changed"]:
            _notify(crd, f"🔄 <b>ADSync: автовыдача синхронизирована.</b>\n\n"
                         f"Группа: <b>{_escape(group['name'])}</b>\n"
                         f"Товаров теперь: <b>{report['total']}</b> "
                         f"(убрано: {report['removed']}, добавлено: {report['added']})\n"
                         f"Обновлено лотов: <b>{len(report['changed'])}</b> из {report['read']}")
    if dirty:
        _save_settings(settings)


def _sync_group_thread(crd: "Cardinal", gid: int, report_to: int | None = None) -> None:
    """Ручная синхронизация одной группы с отчетом в чат."""
    settings = _load_settings()
    group = _get_group(settings, gid)
    if group is None:
        return
    report = sync_group(crd, group)  # изменяет group["baseline"]/["last"] на месте
    _save_settings(settings)         # сохраняем обновленный срез
    if report_to and crd.telegram and report is not None:
        if report["changed"]:
            lines = "\n".join(f"• лот <code>{lid}</code>" for lid in report["changed"])
            body = (f"Обновлено лотов: <b>{len(report['changed'])}</b> из {report['read']}:\n{lines}")
        else:
            body = f"Изменений нет — все лоты ({report['read']}) уже синхронны."
        warn = ""
        if report["no_ad"]:
            ids = ", ".join(str(i) for i in report["no_ad"])
            warn = f"\n\n⚠️ У лотов <code>{ids}</code> не включена встроенная автовыдача FunPay."
        try:
            crd.telegram.bot.send_message(
                report_to, f"🔄 <b>Синхронизация группы «{_escape(group['name'])}»</b>\n\n"
                           f"Товаров: <b>{report['total']}</b> "
                           f"(убрано: {report['removed']}, добавлено: {report['added']})\n{body}{warn}",
                parse_mode="HTML")
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def new_order_handler(c: "Cardinal", e, *args) -> None:
    """BIND_TO_NEW_ORDER: мгновенная синхронизация группы проданного лота."""
    try:
        if not _plugin_enabled(c):
            return
        lot_id = getattr(e, "lot_id", None)
        if lot_id is None:
            return
        settings = _load_settings()
        group = _group_of_lot(settings, lot_id)
        if group is None or not group["enabled"]:
            return
        logger.info(f"ADSync: новый заказ по лоту {lot_id} — запускаю синхронизацию "
                    f"группы «{group['name']}».")
        threading.Thread(target=_sync_group_thread, args=(c, group["id"]), daemon=True).start()
    except Exception:
        logger.error("ADSync: ошибка в обработчике нового заказа.")
        logger.debug("TRACEBACK", exc_info=True)


def _sync_loop(crd: "Cardinal") -> None:
    logger.info("ADSync: периодическая синхронизация запущена.")
    last = 0.0
    while not _stop_event.wait(5):
        try:
            if not _plugin_enabled(crd) or not crd.account.is_initiated:
                last = time.time()
                continue
            interval = _load_settings()["interval"]
            if time.time() - last < interval:
                continue
            last = time.time()
            _sync_all(crd)
        except Exception:
            logger.error("ADSync: ошибка в цикле синхронизации.")
            logger.debug("TRACEBACK", exc_info=True)


def _profile_lots(crd: "Cardinal") -> list:
    profile = getattr(crd, "profile", None)
    if profile is None:
        return []
    try:
        return list(profile.get_sorted_lots(1).values())
    except Exception:
        return []


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _home_text(crd: "Cardinal") -> str:
    settings = _load_settings()
    enabled = sum(1 for g in settings["groups"] if g["enabled"])
    return (f"🔗 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"Одинаковое содержимое встроенной автовыдачи FunPay у всех лотов группы:\n"
            f"• продали на одном лоте → товар убирается со всех остальных;\n"
            f"• добавили товар где-то → он появляется везде.\n\n"
            f"Групп: <b>{len(settings['groups'])}</b> (включено: <b>{enabled}</b>)\n"
            f"⏱ Интервал проверки: <b>{settings['interval']}</b> сек.")


def _home_kb(crd: "Cardinal") -> K:
    settings = _load_settings()
    kb = K(row_width=1)
    for g in settings["groups"]:
        mark = "✅" if g["enabled"] else "⛔"
        kb.add(B(f"🔗 {_short(g['name'], 32)} — {len(g['lots'])} лот. {mark}", None,
                 f"{CB_GROUP}:{g['id']}"))
    kb.add(B("➕ Создать группу", None, CB_GADD))
    kb.add(B(f"⏱ Интервал: {settings['interval']}с", None, CB_INT))
    kb.add(B("🔄 Обновить меню", None, CB_HOME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _group_text(crd: "Cardinal", group: dict) -> str:
    last = group.get("last") or {}
    if last.get("ts"):
        last_str = (datetime.fromtimestamp(last["ts"]).strftime("%d.%m %H:%M:%S")
                    + f" (обновлено лотов: {last.get('changed', '?')})")
    else:
        last_str = "еще не выполнялась"
    total = last.get("total")
    total_str = f"\n📦 Товаров в группе: <b>{total}</b>" if total is not None else ""
    text = (f"🔗 <b>Группа «{_escape(group['name'])}»</b>\n\n"
            f"Статус: <b>{'✅ включена' if group['enabled'] else '⛔ выключена'}</b>\n"
            f"Лотов: <b>{len(group['lots'])}</b>{total_str}\n"
            f"Последняя синхронизация: <b>{_escape(last_str)}</b>\n\n")
    if group["lots"]:
        text += "<b>Лоты группы:</b>\n"
        for lid in group["lots"]:
            text += f"• <code>{lid}</code>\n"
    else:
        text += "<i>Лотов пока нет — добавьте через «🎮 Лоты группы».\n" \
                "Первый добавленный лот станет источником товаров для остальных.</i>\n"
    no_ad = last.get("no_ad") or []
    if no_ad:
        ids = ", ".join(str(i) for i in no_ad)
        text += f"\n⚠️ Не включена автовыдача FunPay у лотов: <code>{ids}</code>"
    return text


def _group_kb(group: dict) -> K:
    kb = K(row_width=2)
    kb.add(B("🎮 Лоты группы", None, f"{CB_PICK}:{group['id']}:0"))
    kb.add(B("🔄 Синхронизировать сейчас", None, f"{CB_GSYNC}:{group['id']}"))
    kb.add(B("✅ Включена" if group["enabled"] else "⛔ Выключена", None, f"{CB_GTOG}:{group['id']}"))
    kb.add(B("✏️ Переименовать", None, f"{CB_GNAME}:{group['id']}"))
    kb.add(B("🗑 Удалить группу", None, f"{CB_GDEL}:{group['id']}"))
    kb.add(B("🔙 К списку групп", None, CB_HOME))
    return kb


def _pick_kb(crd: "Cardinal", group: dict, settings: dict, offset: int) -> K:
    lots = _profile_lots(crd)
    kb = K(row_width=1)
    in_group = set(group["lots"])
    for lot in lots[offset:offset + PER_PAGE]:
        lid = int(lot.id)
        if lid in in_group:
            mark = "✅"
        elif _group_of_lot(settings, lid) is not None:
            mark = "🔒"
        else:
            mark = "⬜"
        desc = ", ".join([i for i in [getattr(lot, "server", None), getattr(lot, "side", None),
                                      getattr(lot, "description", None)] if i])
        kb.add(B(f"{mark} {lid} — {_short(desc, 26)}", None, f"{CB_PTOG}:{group['id']}:{lid}:{offset}"))
    pages = max(1, -(-len(lots) // PER_PAGE))
    if pages > 1:
        row = []
        if offset > 0:
            row.append(B("⬅️", None, f"{CB_PICK}:{group['id']}:{offset - PER_PAGE}"))
        row.append(B(f"{offset // PER_PAGE + 1}/{pages}", None, CB_NOOP))
        if offset + PER_PAGE < len(lots):
            row.append(B("➡️", None, f"{CB_PICK}:{group['id']}:{offset + PER_PAGE}"))
        kb.row(*row)
    kb.add(B("✔️ Готово", None, f"{CB_GROUP}:{group['id']}"))
    return kb


def _pick_text(crd: "Cardinal", group: dict) -> str:
    return (f"🎮 <b>Лоты группы «{_escape(group['name'])}»</b>\n\n"
            f"✅ — в группе | ⬜ — добавить | 🔒 — уже в другой группе\n\n"
            f"<i>При добавлении лота его товары сразу заменяются общим списком группы.\n"
            f"Первый лот новой группы задает исходный набор товаров.</i>")


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        def _gid(c: CallbackQuery) -> int:
            return int(str(c.data).split(":")[2])

        # ---------- главное меню ----------
        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb(crd))

        # ---------- группы ----------
        def add_group(c: CallbackQuery) -> None:
            settings = _load_settings()
            gid = _next_group_id(settings)
            settings["groups"].append({"id": gid, "name": f"Группа {gid}", "enabled": True,
                                       "lots": [], "baseline": None, "last": {}})
            _save_settings(settings)
            group = _get_group(settings, gid)
            _answer(bot, c, "➕ Группа создана. Добавьте в неё лоты.")
            _edit_or_send(bot, c, _group_text(crd, group), _group_kb(group))

        def open_group(c: CallbackQuery) -> None:
            _answer(bot, c)
            settings = _load_settings()
            group = _get_group(settings, _gid(c))
            if group is None:
                _edit_or_send(bot, c, _home_text(crd), _home_kb(crd))
                return
            _edit_or_send(bot, c, _group_text(crd, group), _group_kb(group))

        def toggle_group(c: CallbackQuery) -> None:
            settings = _load_settings()
            group = _get_group(settings, _gid(c))
            if group is None:
                _answer(bot, c)
                return
            group["enabled"] = not group["enabled"]
            _save_settings(settings)
            _answer(bot, c, f"Группа «{group['name']}»: "
                            f"{'✅ включена' if group['enabled'] else '⛔ выключена'}")
            _edit_or_send(bot, c, _group_text(crd, group), _group_kb(group))

        def ask_del_group(c: CallbackQuery) -> None:
            _answer(bot, c)
            settings = _load_settings()
            group = _get_group(settings, _gid(c))
            if group is None:
                _edit_or_send(bot, c, _home_text(crd), _home_kb(crd))
                return
            kb = K(row_width=2).add(B("✅ Удалить", None, f"{CB_GDELY}:{group['id']}"),
                                    B("❌ Отмена", None, f"{CB_GROUP}:{group['id']}"))
            _edit_or_send(bot, c, f"🗑 Удалить группу «{_escape(group['name'])}»?\n\n"
                                  f"<i>Сами лоты и их товары не пострадают — группа просто "
                                  f"перестанет синхронизироваться.</i>", kb)

        def del_group(c: CallbackQuery) -> None:
            settings = _load_settings()
            gid = _gid(c)
            settings["groups"] = [g for g in settings["groups"] if g["id"] != gid]
            _save_settings(settings)
            _answer(bot, c, "🗑 Группа удалена.")
            _edit_or_send(bot, c, _home_text(crd), _home_kb(crd))

        def sync_now(c: CallbackQuery) -> None:
            settings = _load_settings()
            group = _get_group(settings, _gid(c))
            if group is None or not group["lots"]:
                _answer(bot, c, "В группе нет лотов.", alert=True)
                return
            _answer(bot, c, "🔄 Синхронизирую группу — отчет придет в этот чат.", alert=True)
            threading.Thread(target=_sync_group_thread, args=(crd, group["id"], c.message.chat.id),
                             daemon=True).start()

        # ---------- выбор лотов ----------
        def open_picker(c: CallbackQuery) -> None:
            _answer(bot, c)
            parts = str(c.data).split(":")
            gid = int(parts[2])
            offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            settings = _load_settings()
            group = _get_group(settings, gid)
            if group is None:
                _edit_or_send(bot, c, _home_text(crd), _home_kb(crd))
                return
            if not _profile_lots(crd):
                _edit_or_send(bot, c, "⏳ Профиль с лотами еще не загружен. Попробуйте через минуту.",
                              K().add(B("🔙 К группе", None, f"{CB_GROUP}:{gid}")))
                return
            _edit_or_send(bot, c, _pick_text(crd, group), _pick_kb(crd, group, settings, offset))

        def toggle_lot(c: CallbackQuery) -> None:
            parts = str(c.data).split(":")
            gid, lot_id, offset = int(parts[2]), int(parts[3]), int(parts[4]) if len(parts) > 4 else 0
            settings = _load_settings()
            group = _get_group(settings, gid)
            if group is None:
                _answer(bot, c)
                return
            if lot_id in group["lots"]:
                group["lots"].remove(lot_id)
                _save_settings(settings)
                _answer(bot, c, f"Лот {lot_id} убран из группы (его товары остались без изменений).")
            else:
                other = _group_of_lot(settings, lot_id)
                if other is not None:
                    _answer(bot, c, f"Лот {lot_id} уже в группе «{other['name']}». "
                                    f"Сначала уберите его оттуда.", alert=True)
                    _edit_or_send(bot, c, _pick_text(crd, group), _pick_kb(crd, group, settings, offset))
                    return
                group["lots"].append(lot_id)
                baseline = group.get("baseline")
                _save_settings(settings)
                if isinstance(baseline, list):
                    # сразу приводим новый лот к общему списку группы
                    threading.Thread(target=_adopt_lot, args=(crd, gid, lot_id), daemon=True).start()
                    _answer(bot, c, f"Лот {lot_id} добавлен — его товары заменяются общим "
                                    f"списком группы ({len(baseline)} шт.).")
                else:
                    _answer(bot, c, f"Лот {lot_id} добавлен. Он первый в группе — его товары "
                                    f"станут общим списком при первой синхронизации.")
            settings = _load_settings()
            group = _get_group(settings, gid)
            _edit_or_send(bot, c, _pick_text(crd, group), _pick_kb(crd, group, settings, offset))

        def _adopt_lot(crd_: "Cardinal", gid: int, lot_id: int) -> None:
            with _sync_lock:
                settings = _load_settings()
                group = _get_group(settings, gid)
                if group is None or not isinstance(group.get("baseline"), list):
                    return
                try:
                    fields = crd_.account.get_lot_fields(int(lot_id))
                    if list(getattr(fields, "secrets", []) or []) != group["baseline"]:
                        fields.secrets = list(group["baseline"])
                        crd_.account.save_lot(fields)
                        logger.info(f"ADSync: лот {lot_id} приведен к списку группы «{group['name']}».")
                except Exception:
                    logger.error(f"ADSync: не удалось привести лот {lot_id} к списку группы.")
                    logger.debug("TRACEBACK", exc_info=True)

        # ---------- настройки ----------
        def act_name(c: CallbackQuery) -> None:
            settings = _load_settings()
            group = _get_group(settings, _gid(c))
            if group is None:
                _answer(bot, c)
                return
            result = bot.send_message(c.message.chat.id,
                                      f"✏️ Введите новое название группы (до 60 символов).\n\n"
                                      f"Текущее: <b>{_escape(group['name'])}</b>",
                                      parse_mode="HTML",
                                      reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_GNAME,
                         data={"gid": group["id"]})
            _answer(bot, c)

        def act_interval(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"⏱ Введите интервал периодической синхронизации в секундах "
                f"(<code>{INTERVAL_RANGE[0]}</code> - <code>{INTERVAL_RANGE[1]}</code>).\n\n"
                f"Текущее значение: <b>{s['interval']}</b> сек.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_INT)
            _answer(bot, c)

        def _gid_from_state(m: Message):
            st = tg.get_state(m.chat.id, m.from_user.id) or {}
            gid = (st.get("data") or {}).get("gid")
            if gid is None:
                try:
                    gid = int(str(st.get("state", "")).rsplit(":", 1)[-1])
                except (TypeError, ValueError):
                    gid = None
            return gid

        def _state_eq(m: Message, state: str) -> bool:
            try:
                return tg.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def _state_starts(m: Message, prefix: str) -> bool:
            try:
                st = tg.get_state(m.chat.id, m.from_user.id)
                return bool(st and str(st.get("state", "")).startswith(prefix))
            except Exception:
                return False

        def edit_name(m: Message) -> None:
            gid = _gid_from_state(m)
            tg.clear_state(m.chat.id, m.from_user.id, True)
            name = str(m.text or "").strip()[:60]
            if not name or gid is None:
                bot.reply_to(m, "❌ Пустое название не сохранено.")
                return
            settings = _load_settings()
            group = _get_group(settings, int(gid))
            if group is None:
                bot.reply_to(m, "❌ Группа не найдена.")
                return
            group["name"] = name
            _save_settings(settings)
            bot.reply_to(m, f"✏️ Группа переименована в «{_escape(name)}».", parse_mode="HTML")

        def edit_interval(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            try:
                value = _clamp(int(str(m.text).strip()), *INTERVAL_RANGE)
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число. Попробуйте еще раз.")
                return
            settings = _load_settings()
            settings["interval"] = value
            _save_settings(settings)
            bot.reply_to(m, f"✅ Интервал синхронизации: <b>{value}</b> сек.", parse_mode="HTML")

        # ---------- команда ----------
        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb(crd))

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        # ---------- регистрация ----------
        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(add_group, lambda c: c.data == CB_GADD)
        tg.cbq_handler(open_group, lambda c: c.data.startswith(f"{CB_GROUP}:"))
        tg.cbq_handler(toggle_group, lambda c: c.data.startswith(f"{CB_GTOG}:"))
        tg.cbq_handler(ask_del_group, lambda c: c.data.startswith(f"{CB_GDEL}:"))
        tg.cbq_handler(del_group, lambda c: c.data.startswith(f"{CB_GDELY}:"))
        tg.cbq_handler(sync_now, lambda c: c.data.startswith(f"{CB_GSYNC}:"))
        tg.cbq_handler(open_picker, lambda c: c.data.startswith(f"{CB_PICK}:"))
        tg.cbq_handler(toggle_lot, lambda c: c.data.startswith(f"{CB_PTOG}:"))
        tg.cbq_handler(act_name, lambda c: c.data.startswith(f"{CB_GNAME}:"))
        tg.cbq_handler(act_interval, lambda c: c.data == CB_INT)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_name, func=lambda m: _state_starts(m, f"{ST_GNAME}"))
        tg.msg_handler(edit_interval, func=lambda m: _state_eq(m, ST_INT))
        tg.msg_handler(cmd_open, commands=["adsync"])

        try:
            crd.add_telegram_commands(UUID, [("adsync", "Синхронизация автовыдачи FunPay между лотами.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"ADSync v{VERSION}: Telegram-меню зарегистрировано (команда /adsync).")
    else:
        logger.info(f"ADSync v{VERSION}: Telegram выключен — синхронизация работает в фоне.")

    threading.Thread(target=_sync_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [new_order_handler]
BIND_TO_DELETE = on_delete
