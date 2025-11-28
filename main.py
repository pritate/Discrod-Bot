import discord
from discord.ext import commands, tasks
import re
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os

# ---------------- CONFIG ----------------
ALLOWED_CHANNELS = [1425720821477015553, 1427263126989963264]  # your two channel IDs
PHT = ZoneInfo("Asia/Manila")

ROOM_NAMES = {
    "AP": "Airport",
    "HB": "Harbor",
    "SHB": "Small Harbor",
    "BANDIT": "Bandit Camp",
    "BIO": "Bio-Research Lab",
    "NUC": "Nuclear Plant",
    "MILI": "Military Base",
    "RB": "Rocket Base",
    "CRUDE": "Crude Oil Base",
    "BS SNOW": "Snow Mountain Bomb Shelter",
    "DOCK": "Dock",
    "FACTORY": "Chemical Factory",
}

BOSS_NAMES = {
    "EG": "EG Mutant",
    "AVG": "Avenger",
    "TANK": "Tank",
    "BN": "Bloodnest"
}

CARD_NAMES = {"PCARD": "Purple Card", "BCARD": "Blue Card"}

CARD_LOCATIONS = {
    "BS UP": "Bomb Shelter Upper",
    "BS BOT": "Bomb Shelter Bottom",
    "BS BOTTOM": "Bomb Shelter Bottom",
    "AP": "Airport",
    "HB": "Harbor",
    "SHB": "Small Harbor",
    "NUC": "Nuclear Plant",
    "MILI": "Military Base",
    "BIO": "Bio-Research Lab",
    "BANDIT": "Bandit Camp",
    "RB": "Rocket Base",
    "CRUDE": "Crude Oil Base",
    "BS SNOW": "Snow Mountain Bomb Shelter",
    "DOCK": "Dock",
    "FACTORY": "Chemical Factory",
}

ROLE_TIMEZONES = {
    "PH": "Asia/Manila",
    "IND": "Asia/Kolkata",
    "MY": "Asia/Kuala_Lumpur",
    "RU": "Europe/Moscow",
    "US": "America/New_York",
    "TH": "Asia/Bangkok",
    "AU": "Australia/Brisbane"
}

# ---------------- TRACKING ----------------
user_sent_times = {}        # per-user posted times
global_next_spawn = {}      # (channel_id, spawn_key) -> datetime
spawn_warned = set()        # warned spawns
upcoming_msg_id = {}        # channel_id -> message id
card_auto_extended = set()  # auto-extended BCARDs
spawn_origin_time = {}      # original taken time (PHT)
last_spawn_record = {}      # last taken time for bosses/cards

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ---------------- REGEX / PARSING ----------------
time_regex = re.compile(r"(?i)(\d{1,2}:\d{2}\s*(?:AM|PM))")
word_token = re.compile(r"([A-Za-z]+)")

LOCATION_ALIASES = {
    "BS": "BS BOT",
    "BSUP": "BS UP",
    "BSUPPER": "BS UP",
    "BS BOT": "BS BOT",
    "BSBOT": "BS BOT",
    "BOT": "BS BOT",
    "BOTTOM": "BS BOT",
    "DOWN": "BS BOT",
    "BELOW": "BS BOT",
    "AP": "AP",
    "AIRPORT": "AP",
    "HB": "HB",
    "HARBOR": "HB",
    "NUC": "NUC",
    "NUCLEAR": "NUC",
    "MILI": "MILI",
    "MILITARY": "MILI",
    "BIO": "BIO",
    "BANDIT": "BANDIT",
    "RB": "RB",
    "ROCKET": "RB",
    "ROCKETBASE": "RB",
    "CRUDE": "CRUDE",
    "CRUDEOIL": "CRUDE",
    "CRUDEOILBASE": "CRUDE",
    "BS SNOW": "BS SNOW",
    "SNOW": "BS SNOW",
    "SNOWMOUNTAIN": "BS SNOW",
    "DOCK": "DOCK",
    "FACTORY": "FACTORY",
    "CHEMICALFACTORY": "FACTORY",
    "SHB": "SHB",
}

ROOM_KEYS = set(ROOM_NAMES.keys()) | {v.upper() for v in ROOM_NAMES.values()}
BOSS_KEYS = set(BOSS_NAMES.keys()) | {v.upper() for v in BOSS_NAMES.values()}
CARD_KEYS = set(CARD_NAMES.keys()) | {v.upper() for v in CARD_NAMES.values()}

# ---------------- HELPERS ----------------
def get_member_timezone(member: discord.Member) -> ZoneInfo:
    for role in member.roles:
        key = role.name.upper()
        if key in ROLE_TIMEZONES:
            return ZoneInfo(ROLE_TIMEZONES[key])
    return PHT

def parse_time_string_to_pht(time_str: str, user_tz: ZoneInfo) -> datetime | None:
    try:
        parsed = datetime.strptime(time_str.strip().upper(), "%I:%M %p")
    except ValueError:
        return None

    now_user = datetime.now(user_tz)
    dt_user = now_user.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    dt_pht = dt_user.astimezone(PHT)

    if dt_pht < datetime.now(PHT):
        dt_pht += timedelta(days=1)
    return dt_pht

def calculate_next_spawn(taken_time: datetime, hours: float) -> datetime:
    return taken_time + timedelta(hours=hours)

def unix_ts(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())

def normalize_token(tok: str) -> str:
    return re.sub(r'[^A-Za-z]', '', tok).upper()

def find_time_keyword_location(message: str):
    s = message
    time_m = time_regex.search(s)
    time_str = time_m.group(1).upper() if time_m else None
    tokens = [normalize_token(t) for t in word_token.findall(s)]
    type_key = None
    location_key = None

    for t in tokens:
        if t in CARD_KEYS:
            type_key = t
            break
    if not type_key:
        for t in tokens:
            if t in BOSS_KEYS:
                type_key = t
                break
    if not type_key and "BLOOD" in tokens:
        type_key = "BN"
    if not type_key:
        for t in tokens:
            if t in ROOM_KEYS:
                type_key = t
                break

    for i in range(len(tokens)-1, -1, -1):
        two = (tokens[i-1] + " " + tokens[i]) if i-1 >= 0 else tokens[i]
        if two in LOCATION_ALIASES:
            location_key = LOCATION_ALIASES[two]
            break
        if tokens[i] in LOCATION_ALIASES:
            location_key = LOCATION_ALIASES[tokens[i]]
            break

    if type_key:
        tk = type_key
        rev_room = {v.upper(): k for k, v in ROOM_NAMES.items()}
        rev_boss = {v.upper(): k for k, v in BOSS_NAMES.items()}
        if tk in rev_room:
            type_key = rev_room[tk]
        elif tk in rev_boss:
            type_key = rev_boss[tk]

    return time_str, type_key, location_key

# ---------------- EMBED HELPERS ----------------
GOLD = 0xD4AF37
DARK = 0x0B0B0B

def format_remaining_time(target: datetime, now: datetime) -> str:
    delta = target - now
    if delta.total_seconds() <= 0:
        return "now"
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    return "in " + " ".join(parts)

def build_embed(title: str, description: str, fields: dict, color: int = GOLD):
    embed = discord.Embed(title=title, description=description, color=color)
    for name, value in fields.items():
        embed.add_field(name=name, value=value, inline=False)
    embed.timestamp = datetime.now(tz=timezone.utc)
    return embed

def build_upcoming_embed(channel: discord.TextChannel):
    embed = discord.Embed(title=f"📅 Upcoming Spawns — {channel.name}", color=0x111111)
    rooms, bosses, cards = [], [], []
    now = datetime.now(PHT)

    for (cid, key), spawn_time in global_next_spawn.items():
        if cid != channel.id:
            continue
        remaining_str = format_remaining_time(spawn_time, now)
        ts = unix_ts(spawn_time)
        if key in ROOM_NAMES:
            rooms.append(f"- {ROOM_NAMES[key]} — <t:{ts}:T> ({remaining_str})")
        elif key in BOSS_NAMES:
            bosses.append(f"- {BOSS_NAMES[key]} — <t:{ts}:T> ({remaining_str})")
        elif key.startswith("PCARD") or key.startswith("BCARD"):
            parts = key.split("_", 1)
            type_k = parts[0]
            location = parts[1] if len(parts) > 1 else "Unknown"
            cards.append(f"- {CARD_NAMES.get(type_k, type_k)} ({location}) — <t:{ts}:T> ({remaining_str})")

    if rooms:
        embed.add_field(name="🏠 Rooms", value="\n".join(rooms), inline=False)
    if bosses:
        embed.add_field(name="🛡️ Bosses", value="\n".join(bosses), inline=False)
    if cards:
        embed.add_field(name="🎴 Cards", value="\n".join(cards), inline=False)

    if not rooms and not bosses and not cards:
        embed.description = "No upcoming spawns tracked."
    return embed

async def update_upcoming_message(channel: discord.TextChannel):
    embed = build_upcoming_embed(channel)
    if channel.id in upcoming_msg_id:
        try:
            msg = await channel.fetch_message(upcoming_msg_id[channel.id])
            await msg.edit(embed=embed)
            return
        except discord.NotFound:
            pass
    msg = await channel.send(embed=embed)
    upcoming_msg_id[channel.id] = msg.id

# ---------------- TASKS ----------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    cleanup_expired_messages.start()
    five_minute_warning.start()
    extend_card_time.start()

@tasks.loop(seconds=60)
async def five_minute_warning():
    now = datetime.now(PHT)
    for (cid, key), spawn_time in list(global_next_spawn.items()):
        if (cid, key) in spawn_warned:
            continue
        secs = (spawn_time - now).total_seconds()
        if 0 <= secs <= 300:
            channel = bot.get_channel(cid)
            if not channel:
                continue
            remaining_str = format_remaining_time(spawn_time, now)
            title = "⚠️ Spawn Incoming"
            desc = f"**{key.replace('_', ' ')}** will spawn soon."
            fields = {"ETA": remaining_str}
            embed = build_embed(title, desc, fields, color=GOLD)
            warn_msg = await channel.send(content="@everyone", embed=embed)
            spawn_warned.add((cid, key))
            asyncio.create_task(delete_later(warn_msg, 300))

# ---- rest of your loops and commands remain unchanged ----
# Keep extend_card_time, cleanup_expired_messages, on_message, help, clear etc.
# Just make sure to replace all `<t:...:R>` with `format_remaining_time()` when you want "in Xh Ym"

# ---------------- RUN BOT ----------------
bot.run(os.environ["TOKEN"])
