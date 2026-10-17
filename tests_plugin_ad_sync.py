"""
Тесты плагина ADSync — синхронизация встроенной автовыдачи FunPay (группы лотов).

Запуск из корня FunPayCardinal:  python tests_plugin_ad_sync.py
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import time
import types as _types
from types import SimpleNamespace
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── заглушка telebot (работает независимо от установленного pyTelegramBotAPI) ──
telebot_stub = _types.ModuleType("telebot")
types_stub = _types.ModuleType("telebot.types")


class _Btn:
    def __init__(self, text, _=None, callback_data=None, url=None):
        self.text = text
        self.callback_data = callback_data
        self.url = url


class _Kb:
    def __init__(self, *a, **kw):
        self.buttons = []

    def add(self, *btns):
        self.buttons.extend(btns)
        return self

    def row(self, *btns):
        self.buttons.extend(btns)
        return self


def _answer_cb(*a, **kw):
    pass


types_stub.InlineKeyboardButton = _Btn
types_stub.InlineKeyboardMarkup = _Kb
types_stub.CallbackQuery = SimpleNamespace
types_stub.Message = SimpleNamespace
telebot_stub.types = types_stub
apihelper_stub = _types.ModuleType("telebot.apihelper")


class ApiTelegramException(Exception):
    pass


apihelper_stub.ApiTelegramException = ApiTelegramException
telebot_stub.apihelper = apihelper_stub
sys.modules["telebot"] = telebot_stub
sys.modules["telebot.types"] = types_stub
sys.modules["telebot.apihelper"] = apihelper_stub

# ── загрузка модуля ──
PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")
spec = importlib.util.spec_from_file_location(
    "plugins.ad_sync", os.path.join(PLUGINS_DIR, "ad_sync.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["plugins.ad_sync"] = mod
spec.loader.exec_module(mod)

PASS = FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


# ══════════════════════════════════ МОКИ ════════════════════════════════════════
class MockLotFields:
    def __init__(self, lot_id, secrets, auto_delivery=True):
        self.lot_id = int(lot_id)
        self.secrets = list(secrets)
        self.auto_delivery = auto_delivery


class MockAccount:
    """store: {lot_id: MockLotFields}; errors — лоты, чтение которых падает."""
    is_initiated = True

    def __init__(self, store=None, errors=None):
        self.store = store if store is not None else {}
        self.errors = set(errors or [])
        self.saved = []
        self.get_calls = []

    def get_lot_fields(self, lot_id):
        lot_id = int(lot_id)
        self.get_calls.append(lot_id)
        if lot_id in self.errors:
            raise RuntimeError(f"read error lot {lot_id}")
        if lot_id not in self.store:
            raise RuntimeError(f"lot {lot_id} not found")
        src = self.store[lot_id]
        return MockLotFields(src.lot_id, src.secrets, src.auto_delivery)

    def save_lot(self, fields, *args):
        self.saved.append(int(fields.lot_id))
        self.store[int(fields.lot_id)].secrets = list(fields.secrets)
        return True


class MockProfile:
    def __init__(self, lots):  # {lot_id: description}
        self._lots = {int(i): SimpleNamespace(id=int(i), description=d, server=None, side=None)
                      for i, d in lots.items()}

    def get_sorted_lots(self, page):
        return dict(self._lots)


class MockBot:
    def __init__(self):
        self.messages = []
        self.callback_answers = []
        self._mid = 0

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self._mid += 1
        self.messages.append({"chat_id": chat_id, "text": text,
                              "markup": reply_markup, "id": self._mid})
        return SimpleNamespace(id=self._mid)

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.messages.append({"chat_id": chat_id, "text": text, "markup": reply_markup,
                              "id": message_id, "edit": True})
        return SimpleNamespace(id=message_id)

    def answer_callback_query(self, callback_id, text=None, show_alert=False):
        self.callback_answers.append({"id": callback_id, "text": text, "alert": show_alert})

    def reply_to(self, message, text, parse_mode=None, reply_markup=None):
        self._mid += 1
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        self.messages.append({"chat_id": chat_id, "text": text,
                              "markup": reply_markup, "id": self._mid, "reply": True})
        return SimpleNamespace(id=self._mid)


class MockTelegram:
    """Для фоновых функций (send_notification)."""
    def __init__(self):
        self.bot = MockBot()

    def send_notification(self, text, **kw):
        self.bot.send_message(100, text, parse_mode="HTML")


class TestTG:
    """Telegram-мок для init(): записывает хендлеры и состояния."""
    def __init__(self):
        self.cbq_handlers = []
        self.msg_handlers = []  # (kwargs_dict, func)
        self.bot = MockBot()
        self.states = {}

    def cbq_handler(self, func, func_filter):
        self.cbq_handlers.append((func_filter, func))

    def msg_handler(self, handler, **kw):
        self.msg_handlers.append((kw, handler))

    def set_state(self, chat_id, message_id, user_id, state, data=None, **kw):
        self.states[(chat_id, user_id)] = {"state": state, "data": data or {},
                                           "message_id": message_id}

    def check_state(self, chat_id, user_id, state):
        return (self.states.get((chat_id, user_id)) or {}).get("state") == state

    def get_state(self, chat_id, user_id):
        return self.states.get((chat_id, user_id))

    def clear_state(self, chat_id, user_id, flag=False):
        self.states.pop((chat_id, user_id), None)


class MockCardinal:
    def __init__(self, account=None, profile=None, telegram=True):
        self.account = account if account is not None else MockAccount()
        self.profile = profile if profile is not None else MockProfile({})
        self.telegram = MockTelegram() if telegram else None
        self.plugins = {mod.UUID: SimpleNamespace(enabled=True)}
        self.commands = []

    def add_telegram_commands(self, plugin_uuid, commands):
        self.commands.append((plugin_uuid, commands))


def make_cbq(data, chat_id=100, user_id=200, msg_id=1):
    return SimpleNamespace(id="cb" + str(abs(hash(data)) % 10000), data=data,
                           message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                           from_user=SimpleNamespace(id=user_id))


def make_msg(text, chat_id=100, user_id=200):
    return SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id),
                           from_user=SimpleNamespace(id=user_id))


def find_cbq(tg, prefix=None, exact=None):
    for cbq, fn in tg.cbq_handlers:
        try:
            if exact is not None:
                if cbq(make_cbq(exact)):
                    return fn
            elif prefix is not None and cbq(make_cbq(prefix + "1")):
                return fn
        except Exception:
            continue
    return None


def find_msg_for(tg, m):
    """Ищет msg-хендлер, state-фильтр которого пропускает сообщение m."""
    for kw, fn in tg.msg_handlers:
        f = kw.get("func")
        if f is None:
            continue
        try:
            if f(m):
                return fn
        except Exception:
            continue
    return None


def init_plugin(crd):
    crd.telegram = TestTG()
    mod._stop_event.clear()
    mod.init(crd)


# ══════════════════════════════════ ТЕСТЫ ═══════════════════════════════════════
def section(title):
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def test_metadata():
    section("МЕТАДАННЫЕ ПЛАГИНА")
    check(str(UUID(mod.UUID, version=4)) == mod.UUID, f"UUID валиден ({mod.UUID})")
    check(mod.NAME == "ADSync (FunPay Auto-Delivery Sync)", f"NAME английский: {mod.NAME}")
    check(mod.VERSION == "1.0.0", f"VERSION = {mod.VERSION}")
    check(mod.CREDITS == "yungspider", "CREDITS = yungspider")
    check(mod.BIND_TO_PRE_INIT == [mod.init], "BIND_TO_PRE_INIT = [init]")
    check(mod.BIND_TO_NEW_ORDER == [mod.new_order_handler], "BIND_TO_NEW_ORDER")
    check(callable(mod.on_delete) and mod.BIND_TO_DELETE == mod.on_delete, "BIND_TO_DELETE")
    check("автовыдач" in mod.DESCRIPTION.lower() and "/adsync" in mod.DESCRIPTION, "DESCRIPTION")


def test_merge_algorithm():
    section("АЛГОРИТМ СЛИЯНИЯ (_merge_secrets)")
    merged, rem, add = mod._merge_secrets(["p1", "p2", "p3"], {1: ["p1", "p3"], 2: ["p1", "p2", "p3"]})
    check(merged == ["p1", "p3"] and rem == 1 and add == 0,
          "продажа на одном лоте → товар убран из общего списка")
    merged, rem, add = mod._merge_secrets(["p1"], {1: ["p1"], 2: ["p1", "p2"]})
    check(merged == ["p1", "p2"] and rem == 0 and add == 1, "пополнение на одном лоте → добавлено всем")
    merged, rem, add = mod._merge_secrets(["p1", "p2", "p3"], {1: ["p2", "p3"], 2: ["p1", "p2", "p3", "p4"]})
    check(merged == ["p2", "p3", "p4"] and rem == 1 and add == 1,
          "одновременные продажа и пополнение корректно сливаются")
    merged, rem, add = mod._merge_secrets(["p1"], {1: ["p1", "p2"], 2: ["p1", "p2"]})
    check(merged == ["p1", "p2"] and merged.count("p2") == 1, "дубликаты добавлений не повторяются")
    merged, rem, add = mod._merge_secrets(["p1", "p2"], {1: [], 2: []})
    check(merged == [] and rem == 2, "все товары проданы → общий список пуст")
    merged, rem, add = mod._merge_secrets(["p1"], {1: ["p1", "p2"], 2: ["p1", "p3"]})
    check(merged == ["p1", "p2", "p3"], f"пополнения на разных лотах объединяются: {merged}")


def _setup(settings_data, store, profile_lots=None, errors=None):
    tmp = tempfile.mkdtemp()
    mod.PLUGIN_DIR = os.path.join(tmp, "ad_sync")
    mod.SETTINGS_PATH = os.path.join(mod.PLUGIN_DIR, "settings.json")
    mod._save_settings(settings_data)
    account = MockAccount(store=store, errors=errors)
    crd = MockCardinal(account=account,
                       profile=MockProfile(profile_lots if profile_lots is not None else
                                           {i: f"лот {i}" for i in store}))
    crd.telegram = MockTelegram()
    crd.plugins = {mod.UUID: SimpleNamespace(enabled=True)}
    mod.READ_DELAY = mod.SAVE_DELAY = 0
    return tmp, crd


def _group(lots, baseline=None, enabled=True, gid=1, name="Аккаунты"):
    return {"id": gid, "name": name, "enabled": enabled, "lots": list(lots),
            "baseline": list(baseline) if baseline is not None else None, "last": {}}


def _store(mapping, auto_delivery=True):
    return {int(k): MockLotFields(k, v, auto_delivery) for k, v in mapping.items()}


def test_sync_group():
    section("СИНХРОНИЗАЦИЯ ГРУППЫ (sync_group)")
    # 1. Первый проход: источник — первый лот, без слияния
    store = _store({11: ["a1", "a2"], 22: ["b1"]})
    tmp, crd = _setup({"groups": [_group([11, 22])], "interval": 60}, store)
    try:
        settings = mod._load_settings()
        report = mod.sync_group(crd, settings["groups"][0])
        check(store[11].secrets == ["a1", "a2"] and store[22].secrets == ["a1", "a2"],
              f"первый проход: товары первого лота скопированы остальным (22={store[22].secrets})")
        check(report["changed"] == [22], "изменён только второй лот")
        check(settings["groups"][0]["baseline"] == ["a1", "a2"], "базовый срез сохранён в группе")
    finally:
        shutil.rmtree(tmp)

    # 2. Продажа: товар исчез с одного лота → убирается со всех
    store = _store({11: ["a1", "a2", "a3"], 22: ["a1", "a2", "a3"], 33: ["a1", "a2", "a3"]})
    store[22].secrets = ["a1", "a3"]  # a2 продан на лоте 22 (FunPay списала)
    tmp, crd = _setup({"groups": [_group([11, 22, 33], ["a1", "a2", "a3"])], "interval": 60}, store)
    try:
        settings = mod._load_settings()
        report = mod.sync_group(crd, settings["groups"][0])
        check(all(store[i].secrets == ["a1", "a3"] for i in (11, 22, 33)),
              "проданный товар убран со всех лотов группы")
        check(sorted(report["changed"]) == [11, 33] and report["removed"] == 1,
              f"обновлены лоты без проданного товара, removed=1: {report['changed']}")
        check(settings["groups"][0]["baseline"] == ["a1", "a3"], "baseline обновлён после продажи")
    finally:
        shutil.rmtree(tmp)

    # 3. Пополнение через сайт на одном лоте → расходится на все
    store = _store({11: ["a1"], 22: ["a1", "a2"]})
    tmp, crd = _setup({"groups": [_group([11, 22], ["a1"])], "interval": 60}, store)
    try:
        settings = mod._load_settings()
        report = mod.sync_group(crd, settings["groups"][0])
        check(store[11].secrets == ["a1", "a2"] and report["changed"] == [11] and report["added"] == 1,
              "новый товар добавлен на второй лот")
    finally:
        shutil.rmtree(tmp)

    # 4. Нет изменений → сохранений нет
    store = _store({11: ["a1"], 22: ["a1"]})
    tmp, crd = _setup({"groups": [_group([11, 22], ["a1"])], "interval": 60}, store)
    try:
        settings = mod._load_settings()
        report = mod.sync_group(crd, settings["groups"][0])
        check(report["changed"] == [] and crd.account.saved == [], "без изменений — сохранений нет")
        check(report["read"] == 2, "прочитаны оба лота")
    finally:
        shutil.rmtree(tmp)

    # 5. Лот недоступен (ошибка чтения) → пропускается, ложных удалений нет
    store = _store({11: ["a1", "a2"], 22: ["a1", "a2"]})
    tmp, crd = _setup({"groups": [_group([11, 22, 33], ["a1", "a2"])], "interval": 60},
                      store, errors={33})
    try:
        settings = mod._load_settings()
        report = mod.sync_group(crd, settings["groups"][0])
        check(report["read"] == 2 and report["changed"] == [], "недоступный лот пропущен без удалений")
        check(store[11].secrets == ["a1", "a2"], "товары остальных лотов не тронуты")
    finally:
        shutil.rmtree(tmp)

    # 6. Детект лотов без включённой автовыдачи FunPay
    store = _store({11: ["a1"], 22: ["a1"]}, auto_delivery=True)
    store[22].auto_delivery = False
    tmp, crd = _setup({"groups": [_group([11, 22], ["a1"])], "interval": 60}, store)
    try:
        settings = mod._load_settings()
        report = mod.sync_group(crd, settings["groups"][0])
        check(report["no_ad"] == [22] and settings["groups"][0]["last"]["no_ad"] == [22],
              "лот без автовыдачи отмечен в отчете и last")
    finally:
        shutil.rmtree(tmp)


def test_new_order_sync():
    section("МГНОВЕННАЯ СИНХРОНИЗАЦИЯ ПО НОВОМУ ЗАКАЗУ")
    store = _store({11: ["a1", "a2"], 22: ["a1", "a2"]})
    tmp, crd = _setup({"groups": [_group([11, 22], ["a1", "a2"])], "interval": 60}, store)
    try:
        init_plugin(crd)
        # заказ по лоту 11, товар a1 списан FunPay
        store[11].secrets = ["a2"]
        mod.new_order_handler(crd, SimpleNamespace(lot_id=11))
        for _ in range(50):
            if store[22].secrets == ["a2"]:
                break
            time.sleep(0.1)
        check(store[22].secrets == ["a2"], "после заказа проданный товар убран со второго лота")

        # заказ по лоту не из группы → ничего не происходит
        crd.account.saved.clear()
        mod.new_order_handler(crd, SimpleNamespace(lot_id=999))
        time.sleep(0.3)
        check(crd.account.saved == [], "заказ по лоту вне группы игнорируется")

        # выключенная группа
        settings = mod._load_settings()
        settings["groups"][0]["enabled"] = False
        mod._save_settings(settings)
        mod.new_order_handler(crd, SimpleNamespace(lot_id=11))
        time.sleep(0.3)
        check(crd.account.saved == [], "выключенная группа не синхронизируется")
        settings = mod._load_settings()
        settings["groups"][0]["enabled"] = True
        mod._save_settings(settings)

        # без lot_id / плагин выключен
        mod.new_order_handler(crd, SimpleNamespace())
        crd.plugins[mod.UUID].enabled = False
        store[11].secrets = ["a1", "a2"]
        mod.new_order_handler(crd, SimpleNamespace(lot_id=11))
        time.sleep(0.3)
        check(crd.account.saved == [], "без lot_id и при выключенном плагине — без действий")
    finally:
        crd.plugins[mod.UUID].enabled = True
        shutil.rmtree(tmp)


def test_ui():
    section("TELEGRAM UI (/adsync)")
    store = _store({11: ["a1", "a2"], 22: ["a1", "a2"], 33: ["x"], 44: ["free"]})
    tmp, crd = _setup({"groups": [_group([11, 22], ["a1", "a2"]),
                                  _group([33], ["x"], gid=2, name="Ключи")], "interval": 60},
                      store,
                      profile_lots={11: "Акк Steam", 22: "Акк Steam 2", 33: "Ключ", 44: "Свободный"})
    try:
        init_plugin(crd)
        tg = crd.telegram
        bot = tg.bot

        # --- главное меню ---
        find_cbq(tg, exact=mod.CB_HOME)(make_cbq(mod.CB_HOME))
        txt = bot.messages[-1]["text"]
        check("ADSync" in txt and "Групп: <b>2</b>" in txt and "Интервал" in txt, "главное меню")
        data = [b.callback_data for b in bot.messages[-1]["markup"].buttons if b.callback_data]
        check(f"{mod.CB_GROUP}:1" in data and mod.CB_GADD in data, "кнопки групп и создания")

        # --- создание группы ---
        find_cbq(tg, exact=mod.CB_GADD)(make_cbq(mod.CB_GADD))
        s = mod._load_settings()
        check(len(s["groups"]) == 3 and s["groups"][2]["id"] == 3, "новая группа id=3 создана")

        # --- страница группы ---
        find_cbq(tg, prefix=f"{mod.CB_GROUP}:")(make_cbq(f"{mod.CB_GROUP}:1"))
        txt = bot.messages[-1]["text"]
        check("Аккаунты" in txt and "<code>11</code>" in txt and "<code>22</code>" in txt,
              "страница группы со списком лотов")

        # --- вкл/выкл ---
        find_cbq(tg, prefix=f"{mod.CB_GTOG}:")(make_cbq(f"{mod.CB_GTOG}:2"))
        check(mod._load_settings()["groups"][1]["enabled"] is False, "группа выключена")
        find_cbq(tg, prefix=f"{mod.CB_GTOG}:")(make_cbq(f"{mod.CB_GTOG}:2"))
        check(mod._load_settings()["groups"][1]["enabled"] is True, "группа снова включена")

        # --- выбор лотов: метки ---
        find_cbq(tg, prefix=f"{mod.CB_PICK}:")(make_cbq(f"{mod.CB_PICK}:1:0"))
        btns = [b.text for b in bot.messages[-1]["markup"].buttons
                if b.callback_data and b.callback_data.startswith(mod.CB_PTOG)]
        check(any(b.startswith("✅ 11") for b in btns), "лот группы помечен ✅")
        check(any(b.startswith("🔒 33") for b in btns), "лот другой группы помечен 🔒")
        check(any(b.startswith("⬜ 44") for b in btns), "свободный лот помечен ⬜")

        # --- добавление лота в группу с baseline → его товары заменяются ---
        find_cbq(tg, prefix=f"{mod.CB_PTOG}:")(make_cbq(f"{mod.CB_PTOG}:1:44:0"))
        for _ in range(50):
            if store[44].secrets == ["a1", "a2"]:
                break
            time.sleep(0.1)
        check(44 in mod._load_settings()["groups"][0]["lots"], "лот 44 добавлен в группу 1")
        check(store[44].secrets == ["a1", "a2"], "товары добавленного лота заменены общим списком")

        # --- лот из другой группы не добавляется ---
        bot.callback_answers.clear()
        find_cbq(tg, prefix=f"{mod.CB_PTOG}:")(make_cbq(f"{mod.CB_PTOG}:1:33:0"))
        check(33 not in mod._load_settings()["groups"][0]["lots"], "лот чужой группы не добавлен")
        check(any(a.get("alert") for a in bot.callback_answers), "показано предупреждение (alert)")

        # --- удаление лота из группы ---
        find_cbq(tg, prefix=f"{mod.CB_PTOG}:")(make_cbq(f"{mod.CB_PTOG}:1:44:0"))
        check(44 not in mod._load_settings()["groups"][0]["lots"], "лот 44 убран из группы")

        # --- ручная синхронизация с отчетом ---
        store[22].secrets = ["a1"]  # имитация продажи a2 на лоте 22
        bot.messages.clear()
        find_cbq(tg, prefix=f"{mod.CB_GSYNC}:")(make_cbq(f"{mod.CB_GSYNC}:1"))
        for _ in range(50):
            if any("Синхронизация группы" in m["text"] for m in bot.messages):
                break
            time.sleep(0.1)
        check(store[11].secrets == ["a1"], "ручная синхронизация убрала a2 с лота 11")
        rep = next((m["text"] for m in bot.messages if "Синхронизация группы" in m["text"]), "")
        check("убрано: 1" in rep and "Обновлено лотов" in rep, "отчет о синхронизации в чат")

        # --- переименование ---
        find_cbq(tg, prefix=f"{mod.CB_GNAME}:")(make_cbq(f"{mod.CB_GNAME}:1"))
        check(tg.get_state(100, 200)["state"] == mod.ST_GNAME and
              tg.get_state(100, 200)["data"]["gid"] == 1, "стейт переименования с gid")
        m = make_msg("Пулы аккаунтов")
        h = find_msg_for(tg, m)
        check(h is not None, "найден хендлер переименования по state")
        if h:
            h(m)
        check(mod._load_settings()["groups"][0]["name"] == "Пулы аккаунтов", "группа переименована")

        # --- интервал ---
        find_cbq(tg, exact=mod.CB_INT)(make_cbq(mod.CB_INT))
        check(tg.get_state(100, 200)["state"] == mod.ST_INT, "стейт интервала")
        m = make_msg("9999")
        h = find_msg_for(tg, m)
        check(h is not None, "найден хендлер интервала по state")
        if h:
            h(m)
        check(mod._load_settings()["interval"] == mod.INTERVAL_RANGE[1], "интервал зажат сверху (900)")
        m = make_msg("abc")
        h = find_msg_for(tg, m)
        check(h is None, "после clear_state сообщения без стейта не перехватываются")

        # --- удаление группы ---
        find_cbq(tg, prefix=f"{mod.CB_GDEL}:")(make_cbq(f"{mod.CB_GDEL}:3"))
        check("Удалить группу" in bot.messages[-1]["text"], "подтверждение удаления")
        find_cbq(tg, prefix=f"{mod.CB_GDELY}:")(make_cbq(f"{mod.CB_GDELY}:3"))
        check(len(mod._load_settings()["groups"]) == 2, "группа 3 удалена")

        # --- noop ---
        find_cbq(tg, exact=mod.CB_NOOP)(make_cbq(mod.CB_NOOP))
        check(bool(bot.callback_answers) or True, "noop отвечает без падения")
    finally:
        shutil.rmtree(tmp)


def test_commands_and_registration():
    section("РЕГИСТРАЦИЯ И КОМАНДЫ")
    tmp = tempfile.mkdtemp()
    old_dir, old_path = mod.PLUGIN_DIR, mod.SETTINGS_PATH
    mod.PLUGIN_DIR = os.path.join(tmp, "ad_sync")
    mod.SETTINGS_PATH = os.path.join(mod.PLUGIN_DIR, "settings.json")
    try:
        crd = MockCardinal()
        init_plugin(crd)
        check(len(crd.commands) == 1 and crd.commands[0][1][0][0] == "adsync",
              "/adsync зарегистрирована")
        check(crd.commands[0][0] == mod.UUID, "команда привязана к UUID плагина")
        tg = crd.telegram
        cmd_fn = next((fn for kw, fn in tg.msg_handlers if kw.get("commands") == ["adsync"]), None)
        check(cmd_fn is not None, "msg-хендлер команды /adsync найден")
        if cmd_fn:
            tg.bot.messages.clear()
            cmd_fn(make_msg("/adsync"))
            check("ADSync" in tg.bot.messages[-1]["text"], "/adsync открывает меню")
        fn = find_cbq(tg, exact=f"{mod.CBT.PLUGIN_SETTINGS}:{mod.UUID}")
        check(fn is not None, "вход через PLUGIN_SETTINGS зарегистрирован")
        mod.on_delete(crd)
        check(mod._stop_event.is_set(), "on_delete останавливает цикл")
        mod._stop_event.clear()
    finally:
        mod.PLUGIN_DIR, mod.SETTINGS_PATH = old_dir, old_path
        shutil.rmtree(tmp)


def test_settings_normalization():
    section("НОРМАЛИЗАЦИЯ НАСТРОЕК")
    tmp = tempfile.mkdtemp()
    old_dir, old_path = mod.PLUGIN_DIR, mod.SETTINGS_PATH
    mod.PLUGIN_DIR = os.path.join(tmp, "ad_sync")
    mod.SETTINGS_PATH = os.path.join(mod.PLUGIN_DIR, "settings.json")
    try:
        garbage = {"groups": [
            {"id": "x", "name": None, "lots": "nope", "baseline": "nope", "enabled": 1},
            {"id": 2, "lots": ["11", 11, "bad", 22], "baseline": ["a", 2], "name": 123},
            "string-not-dict",
        ], "interval": "fast", "junk": 1}
        mod._save_settings(garbage)
        s = mod._load_settings()
        check(s["interval"] == mod.DEFAULT_SETTINGS["interval"], "битый интервал → default 60")
        check(len(s["groups"]) == 2, "некорректные элементы списка отброшены")
        g1, g2 = s["groups"]
        check(g1["id"] == 1 and g1["name"] == "Группа 1" and g1["lots"] == [] and
              g1["baseline"] is None, "группа 1 нормализована (имя по id, пустые лоты)")
        check(g2["lots"] == [11, 22], f"лоты: дубли и мусор отброшены, строки→int: {g2['lots']}")
        check(g2["baseline"] == ["a", "2"], "baseline: элементы приведены к строкам")
        check(g2["name"] == "123", "имя приведено к строке")
    finally:
        mod.PLUGIN_DIR, mod.SETTINGS_PATH = old_dir, old_path
        shutil.rmtree(tmp)


def test_sync_all_notification():
    section("ПЕРИОДИЧЕСКИЙ ПРОХОД (_sync_all) И УВЕДОМЛЕНИЯ")
    store = _store({11: ["a1", "a2"], 22: ["a1", "a2"], 33: ["x"], 44: ["x"]})
    store[22].secrets = ["a1"]  # продажа a2 в группе 1
    tmp, crd = _setup({"groups": [_group([11, 22], ["a1", "a2"]),
                                  _group([33, 44], ["x"], gid=2)], "interval": 60}, store)
    try:
        crd.telegram = MockTelegram()
        crd.plugins = {mod.UUID: SimpleNamespace(enabled=True)}
        mod._sync_all(crd)
        check(store[11].secrets == ["a1"], "группа 1: проданный товар убран")
        check(33 not in crd.account.saved and 44 not in crd.account.saved,
              "группа 2 без изменений — не сохранялась")
        msgs = [m["text"] for m in crd.telegram.bot.messages]
        check(len(msgs) == 1 and "Аккаунты" in msgs[0] and "убрано: 1" in msgs[0],
              "одно уведомление — только об изменённой группе")
        s = mod._load_settings()
        check(s["groups"][0]["baseline"] == ["a1"], "baseline группы 1 сохранён после прохода")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_metadata()
    test_merge_algorithm()
    test_sync_group()
    test_new_order_sync()
    test_ui()
    test_commands_and_registration()
    test_settings_normalization()
    test_sync_all_notification()
    print(f"\n{'═' * 70}\n  РЕЗУЛЬТАТ: {PASS} пройдено, {FAIL} провалено\n{'═' * 70}")
    sys.exit(1 if FAIL else 0)
