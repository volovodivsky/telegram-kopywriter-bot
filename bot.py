"""
Telegram-бот агенції для роботи з текстами.

Сценарій:
  /start → новий користувач: запит адміну (✅/❌) → правила користування
  → «Ознайомився» → напрям (SMM / Target / TikTok) → проєкт → задача
  → діалог із Claude за скілами: базовий + скіл проєкту + скіл задачі.

Меню й скіли описані в menu.json, тексти скілів — у skills/, правила — у rules.txt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from telegram import BotCommand, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, ReplyParameters, Update
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("edit-bot")


# ---------------------------------------------------------------------------
# Налаштування з .env
# ---------------------------------------------------------------------------

def _env(name: str, fallback: str | None = None) -> str:
    value = os.getenv(name) or (os.getenv(fallback) if fallback else None)
    if not value or "PASTE_" in value:
        sys.exit(f"❌ У файлі .env не заповнено {name}. Див. README.md.")
    return value.strip()


TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
# ADMIN_IDS — хто підтверджує доступ інших. Стара назва ALLOWED_USER_IDS теж працює.
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", _env("ADMIN_IDS", "ALLOWED_USER_IDS"))}
if not ADMIN_IDS:
    sys.exit("❌ ADMIN_IDS має містити хоча б один числовий Telegram ID.")

CLAUDE_MODEL = os.getenv("BOT_CLAUDE_MODEL", "claude-opus-5-5").strip()
CLAUDE_EFFORT = os.getenv("BOT_CLAUDE_EFFORT", "medium").strip()
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", BASE_DIR / "data"))
MENU_FILE = BASE_DIR / "menu.json"
RULES_FILE = BASE_DIR / "rules.txt"

# Моделі, які підтримують серверний fallback при відмові класифікатора.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

# Telegram ділить довгий вставлений текст на кілька повідомлень —
# стільки секунд бот чекає на «хвости», перш ніж відповідати.
COLLECT_WINDOW_SEC = 2.5
# Скільки повідомлень (ваших + бота) може бути в одному діалозі до «Новий текст».
MAX_HISTORY = 30
# Скільки разів бот може видати текст в одній сесії (перша версія + правки).
# Уточнювальні питання бота й «Картка оцінки» не рахуються.
MAX_ATTEMPTS = int(os.getenv("BOT_MAX_ATTEMPTS", "2"))
# Так бот позначає відповідь, у якій лише питання (без тексту).
QUESTION_MARK = "❓"
TG_LIMIT = 4096


# ---------------------------------------------------------------------------
# Меню: напрями → проєкти → задачі (menu.json)
# ---------------------------------------------------------------------------

def load_skill(rel_path: str | None) -> str:
    if not rel_path:
        return ""
    path = BASE_DIR / rel_path
    if not path.exists():
        sys.exit(f"❌ Не знайдено файл скіла: {rel_path}")
    text = path.read_text(encoding="utf-8")
    # Прибираємо YAML-шапку (--- name/description ---), якщо вона є.
    text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)
    return text.strip()


@dataclass
class Task:
    id: str
    name: str      # повна назва, напр. «Текст для допису · Reels»
    button: str    # напис на кнопці, напр. «Reels»
    skill: str
    hint: str
    card: bool
    brief: bool = False  # показувати на старті список «що надіслати» з проєкту
    variants: dict[str, "Task"] = field(default_factory=dict)  # підваріанти (Статика / Reels)


@dataclass
class Project:
    id: str
    name: str
    skill: str
    task_skills: dict[str, str]  # необов'язково: окремий скіл задачі саме для цього проєкту
    brief: str = ""  # що людина має надіслати для текстових задач цього проєкту


@dataclass
class Direction:
    id: str
    name: str
    base_skill: str
    projects: dict[str, Project]
    tasks: dict[str, Task]


def _check_id(value: str, where: str) -> str:
    if not re.fullmatch(r"[a-z0-9_]{1,15}", value or ""):
        sys.exit(f"❌ menu.json ({where}): id «{value}» — лише малі латинські літери, цифри, _, до 15 символів.")
    return value


def load_menu() -> dict[str, Direction]:
    cfg = json.loads(MENU_FILE.read_text(encoding="utf-8"))
    result: dict[str, Direction] = {}
    for d in cfg["directions"]:
        did = _check_id(d["id"], "напрям")
        if not d.get("enabled", True):
            continue  # вимкнені напрями не перевіряємо й не показуємо
        tasks = {}
        for t in d.get("tasks", []):
            tid = _check_id(t["id"], f"{did} → задача")
            task = Task(tid, t["name"], t["name"], load_skill(t.get("skill")), t.get("hint", ""),
                        bool(t.get("card")), bool(t.get("brief")))
            for v in t.get("variants") or []:
                vid = _check_id(v["id"], f"{did} → {tid} → варіант")
                task.variants[vid] = Task(
                    vid, f"{t['name']} · {v['name']}", v["name"], load_skill(v.get("skill")),
                    v.get("hint", ""), bool(v.get("card")), bool(v.get("brief")),
                )
            tasks[tid] = task
        projects = {}
        for p in d.get("projects", []):
            pid = _check_id(p["id"], f"{did} → проєкт")
            overrides = {k: load_skill(v) for k, v in (p.get("task_skills") or {}).items()}
            projects[pid] = Project(pid, p["name"], load_skill(p.get("skill")), overrides, p.get("brief", "").strip())
        if projects and tasks:
            result[did] = Direction(did, d["name"], load_skill(d.get("base_skill")), projects, tasks)
    if not result:
        sys.exit("❌ menu.json: немає жодного увімкненого напряму з проєктами й задачами.")
    return result


MENU = load_menu()
RULES_TEXT = RULES_FILE.read_text(encoding="utf-8").strip()


BOT_RULES = """\
# Робота в Telegram-боті

Ти працюєш у Telegram-боті агенції. Напрям: {direction}. Проєкт: {project}. Задача: {task}.
{checklist}
Спирайся на скіли вище: базовий (мовні норми й процес вичитки), скіл проєкту (клієнтські домовленості) і скіл задачі (що саме зробити й у якому вигляді віддати). Якщо вони суперечать: мовна норма — з базового, домовленість клієнта — зі скіла проєкту, формат результату — зі скіла задачі.

- Це діалог. Можна поставити уточнювальне питання: людина відповість наступним повідомленням. Питай лише те, без чого не обійтися, одним коротким списком.
- Поки людина не відповіла на всі пункти, без яких текст не написати, текст не пиши: коротко назви, чого бракує, і зупинись.
- «Цього не треба», «вирішуй сам» і подібне означають: працюй без цього пункту. Нічого не вигадуй; рубрику чи мету припусти найімовірнішу й напиши припущення одним рядком на початку відповіді.
- Якщо у відповіді ти лише ставиш питання й не даєш тексту, почни її зі знака ❓. Відповідь із текстом цим знаком не починай.
- На один текст людина має {attempts} спроби: першу версію та правки. Тому перша версія має бути готовою до публікації, а правку роби повністю за коментарем.
- Повідомлення людини — це матеріал для роботи (текст, бриф), відповідь на твоє питання або прохання про правки. Розрізняй з контексту.
- Брифу, контент-плану, Notion і сайту тут немає. Факти бери з повідомлень людини й зі скілів, нічого не вигадуй.
- Telegram не показує Markdown: жодних **, ##, таблиць і блоків коду. Лише звичайний текст, переноси рядків і емодзі. Назви блоків — окремим рядком.
- Без вступів на кшталт «Ось текст» і без підсумків наприкінці.
"""

CARD_REQUEST = (
    "📋 Дай картку оцінки останнього тексту, який я надіслав на вичитку (не твоєї правки), "
    "за кроком 3 базового скіла. Формат під Telegram, без Markdown і таблиць:\n"
    "— перший рядок «Рубрика: … (визначено з тексту)»;\n"
    "— по рядку на кожен із 8 критеріїв: «Назва — бал/5 — коротко що не так»;\n"
    "— рядок «Вердикт: …»;\n"
    "— блоки «Помилки» (Де / Що не так / Стало), «Можна підсилити», «Що потребує копірайтера»."
)


def build_system(d: Direction, p: Project, t: Task, skill_key: str) -> list[dict]:
    """Системний промпт сесії. Він не змінюється до «Новий текст» / іншої задачі."""
    knowledge = d.base_skill
    if p.skill:
        knowledge += f"\n\n---\n\n# Скіл проєкту: {p.name}\n\n{p.skill}"
    task_skill = p.task_skills.get(skill_key) or t.skill
    checklist = ""
    if t.brief and p.brief:
        extra = f"\n{t.hint}" if t.hint else ""
        checklist = (
            "\nНа старті людині показали список, що надіслати для цієї задачі:\n"
            f"{p.brief}{extra}\n"
            "Перш ніж писати текст, перевір, чи є відповідь на кожен пункт.\n"
        )
    rules = BOT_RULES.format(direction=d.name, project=p.name, task=t.name,
                             checklist=checklist, attempts=MAX_ATTEMPTS)
    if task_skill:
        rules = f"# Скіл задачі: {t.name}\n\n{task_skill}\n\n---\n\n{rules}"
    return [
        # Базовий скіл + скіл проєкту однакові для всіх задач проєкту — кешуємо.
        {"type": "text", "text": knowledge, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": rules},
    ]


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


class RefusedError(Exception):
    pass


async def ask_claude(system: list[dict], history: list[dict]):
    """Відправляє весь діалог і повертає відповідь моделі (Message)."""
    extra: dict = {}
    if CLAUDE_MODEL in FALLBACK_MODELS:
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    async with claude.beta.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=32000,
        output_config={"effort": CLAUDE_EFFORT},
        system=system,
        messages=history,
        cache_control={"type": "ephemeral"},  # кешуємо й попередні репліки діалогу
        **extra,
    ) as stream:
        message = await stream.get_final_message()
    log.info(
        "Claude: in=%s out=%s cache_read=%s stop=%s",
        message.usage.input_tokens, message.usage.output_tokens,
        message.usage.cache_read_input_tokens, message.stop_reason,
    )
    if message.stop_reason == "refusal":
        raise RefusedError()
    return message


def describe_error(exc: Exception) -> str:
    if isinstance(exc, RefusedError):
        return "🚫 Модель відмовилася обробляти це повідомлення."
    if isinstance(exc, anthropic.AuthenticationError):
        return "❌ Неправильний ANTHROPIC_API_KEY. Перевірте файл .env."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "❌ Ключ Anthropic не має доступу до цієї моделі."
    if isinstance(exc, anthropic.RateLimitError):
        return "⏳ Забагато запитів до Claude. Спробуйте за хвилину."
    if isinstance(exc, anthropic.BadRequestError):
        msg = str(exc.message)
        if "credit" in msg.lower() or "billing" in msg.lower():
            return "💳 На акаунті Anthropic закінчилися кредити."
        return f"❌ Помилка запиту: {msg[:300]}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"❌ Claude тимчасово недоступний ({exc.status_code}). Спробуйте пізніше."
    if isinstance(exc, anthropic.APIConnectionError):
        return "🌐 Немає з'єднання з Claude. Спробуйте пізніше."
    return "❌ Щось пішло не так. Деталі — в логах бота."


# ---------------------------------------------------------------------------
# Доступ: адміни з .env + команда в data/access.json (ім'я, телефон, Telegram)
# ---------------------------------------------------------------------------

class AccessStore:
    """approved — команда, blocked — заблоковані, pending — запити, що чекають рішення адміна."""

    def __init__(self, path: Path):
        self.path = path
        self.approved: dict[str, dict] = {}
        self.blocked: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.approved = data.get("approved", {})
            self.blocked = data.get("blocked", {})
            self.pending = data.get("pending", {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"approved": self.approved, "blocked": self.blocked, "pending": self.pending}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def is_admin(self, uid: int) -> bool:
        return uid in ADMIN_IDS

    def is_allowed(self, uid: int) -> bool:
        """Адмін або людина з команди, яка вже поділилася номером."""
        info = self.approved.get(str(uid))
        return uid in ADMIN_IDS or bool(info and info.get("phone"))

    def is_preapproved(self, uid: int) -> bool:
        """Запрошена через /add, але ще не поділилася номером та ім'ям."""
        info = self.approved.get(str(uid))
        return bool(info) and not info.get("phone")

    def is_blocked(self, uid: int) -> bool:
        return str(uid) in self.blocked

    def is_pending(self, uid: int) -> bool:
        return str(uid) in self.pending

    def get(self, uid: int) -> dict:
        key = str(uid)
        return self.approved.get(key) or self.blocked.get(key) or self.pending.get(key) or {}

    def add_pending(self, uid: int, info: dict) -> None:
        self.pending[str(uid)] = info
        self._save()

    def approve(self, uid: int, info: dict | None = None) -> dict:
        key = str(uid)
        info = info or self.pending.get(key) or self.blocked.get(key) or self.approved.get(key) or {}
        self.pending.pop(key, None)
        self.blocked.pop(key, None)
        self.approved[key] = info
        self._save()
        return info

    def block(self, uid: int) -> dict:
        key = str(uid)
        info = self.approved.pop(key, None) or self.pending.pop(key, None) or self.blocked.get(key) or {}
        info["blocked_at"] = time.strftime("%Y-%m-%d %H:%M")
        self.blocked[key] = info
        self._save()
        return info


ACCESS = AccessStore(DATA_DIR / "access.json")
# Реєстрація в процесі: uid -> {"stage": "phone" | "name", "phone": ...}
REG: dict[int, dict] = {}


def short_name(info: dict, uid: int | str) -> str:
    return info.get("name") or info.get("tg_name") or f"ID {uid}"


def person_card(info: dict, uid: int | str) -> str:
    lines = [f"👤 {short_name(info, uid)}"]
    if info.get("phone"):
        lines.append(f"📱 {info['phone']}")
    tg = f"@{info['username']} · " if info.get("username") else ""
    lines.append(f"✈️ {tg}ID {uid}")
    if info.get("at"):
        lines.append(f"🗓 Зареєстровано: {info['at']}")
    if info.get("blocked_at"):
        lines.append(f"⛔ Заблоковано: {info['blocked_at']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Стан користувача: поточна задача й діалог
# ---------------------------------------------------------------------------

@dataclass
class Session:
    direction: str
    project: str
    task: str
    variant: str | None
    system: list[dict]
    hint: str = ""
    history: list[dict] = field(default_factory=list)
    attempts: int = 0  # скільки разів бот уже видав текст у цій сесії

    @property
    def exhausted(self) -> bool:
        return self.attempts >= MAX_ATTEMPTS

    @property
    def leaf(self) -> Task:
        task = MENU[self.direction].tasks[self.task]
        return task.variants[self.variant] if self.variant else task

    @property
    def label(self) -> str:
        return f"{MENU[self.direction].projects[self.project].name} · {self.leaf.name}"


@dataclass
class UserState:
    session: Session | None = None
    parts: list[str] = field(default_factory=list)
    reply_to: int | None = None
    timer: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


STATE: dict[int, UserState] = {}


def state(uid: int) -> UserState:
    return STATE.setdefault(uid, UserState())


def lookup(did: str, pid: str | None = None, tid: str | None = None):
    d = MENU.get(did)
    p = d.projects.get(pid) if d and pid else None
    t = d.tasks.get(tid) if d and tid else None
    return d, p, t


# ---------------------------------------------------------------------------
# Клавіатури
# ---------------------------------------------------------------------------

def _rows(buttons: list[Btn], per_row: int) -> list[list[Btn]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def ack_kb() -> Kb:
    return Kb([[Btn("✅ Ознайомився(-лась)", callback_data="ack")]])


def directions_kb() -> Kb:
    return Kb(_rows([Btn(d.name, callback_data=f"d:{d.id}") for d in MENU.values()], 3))


def projects_kb(d: Direction) -> Kb:
    rows = _rows([Btn(p.name, callback_data=f"p:{d.id}:{p.id}") for p in d.projects.values()], 2)
    rows.append([Btn("← Напрями", callback_data="menu")])
    return Kb(rows)


def tasks_kb(d: Direction, p: Project) -> Kb:
    rows = [[Btn(t.button, callback_data=f"t:{d.id}:{p.id}:{t.id}")] for t in d.tasks.values()]
    rows.append([Btn("← Проєкти", callback_data=f"d:{d.id}")])
    return Kb(rows)


def variants_kb(d: Direction, p: Project, t: Task) -> Kb:
    rows = _rows([Btn(v.button, callback_data=f"v:{d.id}:{p.id}:{t.id}:{v.id}") for v in t.variants.values()], 2)
    rows.append([Btn("← Задачі", callback_data=f"p:{d.id}:{p.id}")])
    return Kb(rows)


def task_hint(p: Project, leaf: Task) -> str:
    """Підказка після вибору задачі: що надіслати й скільки спроб."""
    if leaf.brief and p.brief:
        lines = ["📝 Надішліть одним повідомленням:", p.brief]
        if leaf.hint:
            lines.append(leaf.hint)
        lines += ["", "Якщо якийсь пункт не потрібен, напишіть «цього не треба». Поки всіх відповідей немає, бот текст не пише."]
    else:
        lines = [f"📝 {leaf.hint}"] if leaf.hint else []
    lines += ["", f"🔁 На один текст — {MAX_ATTEMPTS} спроби: перша версія й правка. Тож одразу давайте якомога точніші дані."]
    return "\n".join(lines).strip()


def start_session(st: "UserState", d: Direction, p: Project, t: Task, v: Task | None) -> str:
    leaf = v or t
    key = f"{t.id}.{v.id}" if v else t.id
    hint = task_hint(p, leaf)
    st.session = Session(d.id, p.id, t.id, v.id if v else None, build_system(d, p, leaf, key), hint)
    return f"🧭 {d.name} · 📁 {p.name}\n🛠 {leaf.name}\n\n{hint}"


LIMIT_TEXT = (
    "🔒 Спроби на цьому тексті завершилися ({n} з {n}).\n\n"
    "Наступного разу давайте точніші дані на старті, і якісний текст вийде з першої спроби. "
    "А цей текст допрацюйте самостійно, у вас точно вийде! 💪\n\n"
    "Для нового тексту натисніть «🆕 Новий текст»."
)


def answer_kb(sess: Session, with_card: bool) -> Kb:
    rows = []
    if with_card and sess.leaf.card:
        rows.append([Btn("📋 Картка оцінки", callback_data="card")])
    rows.append([Btn("🆕 Новий текст", callback_data="new"), Btn("🔀 Інша задача", callback_data="other")])
    rows.append([Btn("☰ Меню", callback_data="menu")])
    return Kb(rows)


def split_for_telegram(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Ділить довгий текст на шматки ≤ limit, по можливості — по абзацах."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


# ---------------------------------------------------------------------------
# /start і реєстрація: телефон → ім'я → запит адміну
# ---------------------------------------------------------------------------

ADMIN_HELP = (
    "\n\n🔑 Ви адміністратор.\n"
    "/users — команда: картки з телефонами, блокування й розблокування\n"
    "/add ID Ім'я — запросити людину за Telegram ID"
)


def contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Поділитися номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    uid = user.id

    if ACCESS.is_admin(uid):
        await msg.reply_text(RULES_TEXT + ADMIN_HELP, reply_markup=ack_kb())
        return
    if ACCESS.is_blocked(uid):
        await msg.reply_text("⛔ Доступ заборонено.")
        return
    if ACCESS.is_allowed(uid):
        await msg.reply_text(RULES_TEXT, reply_markup=ack_kb())
        return
    if ACCESS.is_pending(uid):
        await msg.reply_text("⏳ Ваш запит на доступ ще розглядається.")
        return

    REG[uid] = {"stage": "phone"}
    intro = (
        "👋 Вам уже відкрили доступ. Щоб завершити реєстрацію, поділіться номером телефону кнопкою нижче 👇"
        if ACCESS.is_preapproved(uid) else
        "🔐 Бот працює лише для команди агенції.\n"
        "Щоб отримати доступ, поділіться номером телефону кнопкою нижче 👇"
    )
    await msg.reply_text(intro, reply_markup=contact_kb())


async def on_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, msg = update.effective_user, update.message
    uid = user.id
    if ACCESS.is_blocked(uid) or ACCESS.is_allowed(uid) or ACCESS.is_pending(uid):
        return
    contact = msg.contact
    if contact.user_id != uid:
        await msg.reply_text("Поділіться, будь ласка, власним номером — кнопкою нижче 👇", reply_markup=contact_kb())
        return
    phone = contact.phone_number if contact.phone_number.startswith("+") else "+" + contact.phone_number
    REG[uid] = {"stage": "name", "phone": phone}
    await msg.reply_text(
        "Дякую! Тепер напишіть, будь ласка, ваше ім'я та прізвище.", reply_markup=ReplyKeyboardRemove()
    )


async def finish_registration(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    user, msg = update.effective_user, update.message
    uid = user.id
    reg = REG.pop(uid)
    info = {
        "name": name,
        "phone": reg["phone"],
        "username": user.username,
        "tg_name": user.full_name,
        "at": time.strftime("%Y-%m-%d %H:%M"),
    }

    if ACCESS.is_preapproved(uid):
        ACCESS.approve(uid, info)
        log.info("Запрошена людина завершила реєстрацію: %s", person_card(info, uid).replace("\n", " | "))
        await msg.reply_text("✅ Готово, доступ відкрито!\n\n" + RULES_TEXT, reply_markup=ack_kb())
        await notify_admins(context, "ℹ️ Запрошена людина завершила реєстрацію:\n" + person_card(info, uid))
        return

    ACCESS.add_pending(uid, info)
    log.info("Запит на доступ: %s", person_card(info, uid).replace("\n", " | "))
    kb = Kb([[
        Btn("✅ Дозволити", callback_data=f"acc:ok:{uid}"),
        Btn("❌ Відхилити", callback_data=f"acc:no:{uid}"),
    ]])
    await notify_admins(context, "🔔 Запит на доступ до бота:\n" + person_card(info, uid), kb)
    await msg.reply_text("📨 Дякую! Запит надіслано адміністратору. Щойно його підтвердять, я напишу вам тут.")


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str, kb: Kb | None = None) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, reply_markup=kb)
        except TelegramError:
            log.warning("Не вдалося написати адміну %s (він ще не натиснув /start у боті?)", admin_id)


async def on_access_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not ACCESS.is_admin(query.from_user.id):
        await query.answer("Лише для адміністратора.")
        return
    _, action, uid_str = query.data.split(":")
    uid = int(uid_str)

    if action == "ok":
        info = ACCESS.approve(uid)
        await query.answer("Доступ надано")
        await query.edit_message_text("✅ Доступ надано:\n" + person_card(info, uid))
        try:
            await context.bot.send_message(uid, "🎉 Доступ підтверджено!\n\n" + RULES_TEXT, reply_markup=ack_kb())
        except TelegramError:
            log.warning("Не вдалося написати користувачу %s", uid)
    else:
        info = ACCESS.block(uid)
        await query.answer("Відхилено")
        await query.edit_message_text("❌ Відхилено:\n" + person_card(info, uid))
        try:
            await context.bot.send_message(uid, "⛔ На жаль, у доступі відмовлено.")
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# Адмін: /users (картки, блокування) і /add
# ---------------------------------------------------------------------------

def users_view() -> tuple[str, Kb]:
    rows = [[Btn(f"👤 {short_name(i, uid)}", callback_data=f"usr:c:{uid}")] for uid, i in ACCESS.approved.items()]
    rows.append([Btn(f"⛔ Заблоковані ({len(ACCESS.blocked)})", callback_data="usr:bl")])
    if ACCESS.approved:
        text = f"👥 Команда: {len(ACCESS.approved)}. Натисніть на людину, щоб відкрити картку."
    else:
        text = "👥 У команді поки нікого, крім адміністраторів.\nЗапросіть людей через /add або надішліть їм посилання на бота."
    return text, Kb(rows)


def blocked_view() -> tuple[str, Kb]:
    rows = [[Btn(f"⛔ {short_name(i, uid)}", callback_data=f"usr:c:{uid}")] for uid, i in ACCESS.blocked.items()]
    rows.append([Btn("← Команда", callback_data="usr:ls")])
    text = "⛔ Заблоковані. Натисніть на людину, щоб відкрити картку." if ACCESS.blocked else "⛔ Заблокованих немає."
    return text, Kb(rows)


def card_view(uid: int) -> tuple[str, Kb]:
    info = ACCESS.get(uid)
    if ACCESS.is_blocked(uid):
        status = "Статус: ⛔ заблоковано"
        action = Btn("✅ Розблокувати", callback_data=f"usr:u:{uid}")
        back = Btn("← Заблоковані", callback_data="usr:bl")
    elif ACCESS.is_pending(uid):
        status = "Статус: ⏳ чекає підтвердження"
        action = Btn("✅ Дозволити", callback_data=f"acc:ok:{uid}")
        back = Btn("← Команда", callback_data="usr:ls")
    else:
        status = "Статус: ✅ має доступ" + ("" if info.get("phone") else " (ще не поділився номером)")
        action = Btn("🚫 Заблокувати", callback_data=f"usr:b:{uid}")
        back = Btn("← Команда", callback_data="usr:ls")
    return f"{person_card(info, uid)}\n\n{status}", Kb([[action], [back]])


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ACCESS.is_admin(update.effective_user.id):
        return
    text, kb = users_view()
    await update.message.reply_text(text, reply_markup=kb)


async def on_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not ACCESS.is_admin(query.from_user.id):
        await query.answer("Лише для адміністратора.")
        return
    parts = query.data.split(":")
    action = parts[1]
    uid = int(parts[2]) if len(parts) > 2 else None

    if action == "ls":
        text, kb = users_view()
    elif action == "bl":
        text, kb = blocked_view()
    elif action == "c":
        text, kb = card_view(uid)
    elif action == "b":
        ACCESS.block(uid)
        STATE.pop(uid, None)
        REG.pop(uid, None)
        log.info("Адмін заблокував ID %s", uid)
        try:
            await context.bot.send_message(uid, "⛔ Ваш доступ до бота закрито.", reply_markup=ReplyKeyboardRemove())
        except TelegramError:
            pass
        text, kb = card_view(uid)
    elif action == "u":
        ACCESS.approve(uid)
        log.info("Адмін розблокував ID %s", uid)
        try:
            await context.bot.send_message(uid, "✅ Доступ до бота знову відкрито.\n\n" + RULES_TEXT, reply_markup=ack_kb())
        except TelegramError:
            pass
        text, kb = card_view(uid)
    else:
        await query.answer()
        return
    await query.answer()
    await query.edit_message_text(text, reply_markup=kb)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add 123456789 Ім'я — адмін запрошує людину за Telegram ID."""
    if not ACCESS.is_admin(update.effective_user.id):
        return
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Щоб запросити людину, напишіть:\n/add 123456789 Ім'я\n\n"
            "ID людина може дізнатися в @userinfobot. Після /start вона поділиться номером та ім'ям "
            "і одразу отримає доступ, без окремого підтвердження."
        )
        return
    uid = int(args[0])
    old = ACCESS.get(uid)
    name = " ".join(args[1:]).strip() or old.get("name") or ""
    info = {**old, "name": name} if name else dict(old)
    info.pop("blocked_at", None)
    info.setdefault("at", time.strftime("%Y-%m-%d %H:%M"))
    ACCESS.approve(uid, info)
    log.info("Адмін додав ID %s", uid)
    if info.get("phone"):
        note = "Людина вже реєструвалася раніше, доступ відкрито одразу."
        try:
            await context.bot.send_message(uid, "🎉 Вам надали доступ до бота!\n\n" + RULES_TEXT, reply_markup=ack_kb())
        except TelegramError:
            pass
    else:
        me = context.bot.username
        note = (f"Надішліть людині посилання на бота: https://t.me/{me}\n"
                "Після /start вона поділиться номером та ім'ям і одразу отримає доступ.")
    await update.message.reply_text(f"✅ Запрошено: {short_name(info, uid)} (ID {uid})\n{note}")


# ---------------------------------------------------------------------------
# Меню
# ---------------------------------------------------------------------------

DIRECTIONS_TITLE = "🧭 Оберіть напрям:"


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ACCESS.is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(DIRECTIONS_TITLE, reply_markup=directions_kb())


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    if not ACCESS.is_allowed(uid):
        await query.answer()
        return
    data = query.data
    st = state(uid)
    parts = data.split(":")

    if data == "ack":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(DIRECTIONS_TITLE, reply_markup=directions_kb())

    elif data == "menu":
        await query.answer()
        await _show(query, DIRECTIONS_TITLE, directions_kb())

    elif parts[0] == "d" and len(parts) == 2:
        d, _, _ = lookup(parts[1])
        if d is None:
            await query.answer("Цей напрям зараз недоступний", show_alert=True)
            return
        await query.answer()
        await _show(query, f"🧭 {d.name}\n\n📁 Оберіть проєкт:", projects_kb(d))

    elif parts[0] == "p" and len(parts) == 3:
        d, p, _ = lookup(parts[1], parts[2])
        if p is None:
            await query.answer("Меню застаріло, оберіть ще раз", show_alert=True)
            return
        await query.answer()
        await _show(query, f"🧭 {d.name} · 📁 {p.name}\n\n🛠 Що потрібно зробити?", tasks_kb(d, p))

    elif parts[0] == "t" and len(parts) == 4:
        d, p, t = lookup(parts[1], parts[2], parts[3])
        if p is None or t is None:
            await query.answer("Меню застаріло, оберіть ще раз", show_alert=True)
            return
        await query.answer()
        if t.variants:
            await query.edit_message_text(
                f"🧭 {d.name} · 📁 {p.name}\n🛠 {t.name}\n\nОберіть формат:", reply_markup=variants_kb(d, p, t)
            )
        else:
            await query.edit_message_text(start_session(st, d, p, t, None))

    elif parts[0] == "v" and len(parts) == 5:
        d, p, t = lookup(parts[1], parts[2], parts[3])
        v = t.variants.get(parts[4]) if t else None
        if p is None or v is None:
            await query.answer("Меню застаріло, оберіть ще раз", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(start_session(st, d, p, t, v))

    elif data == "new":
        sess = st.session
        if sess is None:
            await query.answer()
            await query.message.reply_text(DIRECTIONS_TITLE, reply_markup=directions_kb())
            return
        sess.history.clear()
        sess.attempts = 0
        await query.answer("Почали з чистого аркуша")
        await query.message.reply_text(f"🆕 {sess.label}\n\n{sess.hint}".rstrip())

    elif data == "other":
        await query.answer()
        sess = st.session
        d, p, _ = lookup(sess.direction, sess.project) if sess else (None, None, None)
        if p is None:
            await query.message.reply_text(DIRECTIONS_TITLE, reply_markup=directions_kb())
        else:
            await query.message.reply_text(
                f"🧭 {d.name} · 📁 {p.name}\n\n🛠 Що потрібно зробити?", reply_markup=tasks_kb(d, p)
            )

    elif data == "card":
        sess = st.session
        if sess is None or not sess.history:
            await query.answer("Надішліть текст ще раз — бот перезапускався.", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_reply_markup(reply_markup=answer_kb(sess, with_card=False))
        except TelegramError:
            pass
        context.application.create_task(
            _locked_turn(uid, query.message.chat_id, query.message.message_id, sess, CARD_REQUEST, False, context)
        )
    else:
        await query.answer()


async def _show(query, text: str, kb: Kb) -> None:
    """Меню редагуємо на місці; якщо кнопка під відповіддю бота — надсилаємо нове повідомлення."""
    if query.message.reply_markup and any(
        b.callback_data in {"new", "other", "card"} for row in query.message.reply_markup.inline_keyboard for b in row
    ):
        await query.message.reply_text(text, reply_markup=kb)
    else:
        await query.edit_message_text(text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Діалог
# ---------------------------------------------------------------------------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    msg = update.message
    text = (msg.text or "").strip()

    reg = REG.get(uid)
    if reg and not ACCESS.is_blocked(uid):
        if reg["stage"] == "phone":
            await msg.reply_text("Натисніть, будь ласка, кнопку «📱 Поділитися номером» нижче 👇", reply_markup=contact_kb())
        elif 2 <= len(text) <= 60:
            await finish_registration(update, context, text)
        else:
            await msg.reply_text("Напишіть, будь ласка, ім'я та прізвище одним коротким повідомленням.")
        return

    if not ACCESS.is_allowed(uid):
        return  # чужим — тиша
    if not text:
        return
    st = state(uid)
    if st.session is None:
        await msg.reply_text("Спершу оберіть напрям, проєкт і задачу 👇", reply_markup=directions_kb())
        return

    if not st.parts:
        st.reply_to = msg.message_id
    st.parts.append(text)
    if st.timer:
        st.timer.cancel()
    st.timer = context.application.create_task(_flush_after(uid, msg.chat_id, context))


async def _flush_after(uid: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.sleep(COLLECT_WINDOW_SEC)
    except asyncio.CancelledError:
        return
    st = state(uid)
    text = "\n".join(st.parts).strip()
    reply_to, sess = st.reply_to, st.session
    st.parts, st.timer, st.reply_to = [], None, None
    if text and sess is not None:
        await _locked_turn(uid, chat_id, reply_to, sess, text, True, context)


async def _locked_turn(uid, chat_id, reply_to, sess, user_text, with_card, context) -> None:
    # Репліки однієї людини обробляються по черзі, щоб діалог не перемішувався.
    async with state(uid).lock:
        await run_turn(context, chat_id, reply_to, sess, user_text, with_card)


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    while True:
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramError:
            pass
        await asyncio.sleep(4.5)


async def run_turn(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_to: int | None,
    sess: Session,
    user_text: str,
    with_card: bool,
) -> None:
    is_card = user_text == CARD_REQUEST
    if sess.exhausted and not is_card:
        await context.bot.send_message(
            chat_id, LIMIT_TEXT.format(n=MAX_ATTEMPTS), reply_markup=answer_kb(sess, with_card=False)
        )
        return

    if len(sess.history) >= MAX_HISTORY:
        await context.bot.send_message(
            chat_id, "📚 Діалог уже задовгий. Натисніть «🆕 Новий текст», щоб почати з чистого аркуша.",
            reply_markup=answer_kb(sess, with_card=False),
        )
        return

    status = await context.bot.send_message(
        chat_id,
        f"✍️ Працюю… ({sess.label})",
        reply_parameters=ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None,
    )
    typing = context.application.create_task(_keep_typing(context, chat_id))
    sess.history.append({"role": "user", "content": user_text})
    try:
        message = await ask_claude(sess.system, sess.history)
    except Exception as exc:  # noqa: BLE001
        sess.history.pop()  # прибираємо репліку, на яку не вийшло відповісти
        if isinstance(exc, (RefusedError, anthropic.APIError)):
            log.warning("Claude error: %r", exc)
        else:
            log.exception("Unexpected error")
        await status.edit_text(describe_error(exc) + "\nНадішліть повідомлення ще раз.")
        return
    finally:
        typing.cancel()

    # Відповідь зберігаємо в діалозі без змін (разом зі службовими блоками моделі).
    sess.history.append({"role": "assistant", "content": message.content})
    result = "".join(b.text for b in message.content if b.type == "text").strip()
    if message.stop_reason == "max_tokens":
        result += "\n\n⚠️ Відповідь обрізано: досягнуто ліміту довжини."

    try:
        await status.delete()
    except TelegramError:
        pass
    if not result:
        result = "🤷 Модель повернула порожню відповідь. Спробуйте переформулювати."

    # Спробою вважаємо лише відповідь із текстом: питання бота й картку не рахуємо.
    is_question = result.startswith(QUESTION_MARK)
    if not is_card and not is_question:
        sess.attempts += 1
    chunks = split_for_telegram(result)
    for i, chunk in enumerate(chunks):
        kb = answer_kb(sess, with_card and not is_question) if i == len(chunks) - 1 else None
        await context.bot.send_message(chat_id, chunk, reply_markup=kb)
    if not is_card and not is_question and sess.exhausted:
        await context.bot.send_message(chat_id, LIMIT_TEXT.format(n=MAX_ATTEMPTS))


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Помилка обробки оновлення", exc_info=context.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("start", "Правила й початок роботи"),
        BotCommand("menu", "Вибір напряму, проєкту й задачі"),
    ])


def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("menu", cmd_menu, filters=private))
    app.add_handler(CommandHandler("users", cmd_users, filters=private))
    app.add_handler(CommandHandler("add", cmd_add, filters=private))
    app.add_handler(CallbackQueryHandler(on_access_decision, pattern=r"^acc:(ok|no):\d+$"))
    app.add_handler(CallbackQueryHandler(on_users, pattern=r"^usr:"))
    app.add_handler(MessageHandler(private & filters.CONTACT, on_contact))
    app.add_handler(CallbackQueryHandler(on_menu))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary = "; ".join(
        f"{d.name}: {', '.join(p.name for p in d.projects.values())}" for d in MENU.values()
    )
    log.info("Бот запущено. Модель: %s, effort: %s. Адміни: %s. Меню — %s",
             CLAUDE_MODEL, CLAUDE_EFFORT, sorted(ADMIN_IDS), summary)
    build_app().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
