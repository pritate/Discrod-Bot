import discord
from discord.ext import commands, tasks
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os

# ---------------- CONFIG ----------------
TARGET_CHANNEL_ID = 1425720821477015553  # Replace with your channel ID
PHT = ZoneInfo("Asia/Manila")

ROOM_NAMES = {
    "AP": "Airport",
    "HB": "Harbor",
    "BANDIT": "Bandit Camp",
    "BIO": "Bio-Research Lab",
    "NUC": "Nuclear Plant",
    "MILI": "Military Base"
}

BOSS_NAMES = {
    "EG": "EG Mutant",
    "AVG": "Avenger",
    "TANK": "Tank"
}

CARD_NAMES = {
    "PCARD": "Pcard",
    "BCARD": "Bcard"
}

CARD_LOCATIONS = {
    "BS UP": "Bomb Shelter Upper",
    "BS BOT": "Bomb Shelter Bottom",
    "AP": "Airport",
    "HB": "Harbor",
    "NUC": "Nuclear Plant",
    "MILI": "Military Base",
    "BIO": "Bio-Research Lab",
    "BANDIT": "Bandit Camp"
}

ROLE_TIMEZONES = {
    "PH": "Asia/Manila",
    "IND": "Asia/Kolkata",
    "MY": "Asia/Kuala_Lumpur",
    "RU": "Europe/Moscow",
    "US": "America/New_York"
}

# ---------------- TRACKING ----------------
user_sent_times = {}
global_next_spawn = {}
spawn_origin_times = {}
upcoming_msg_id = None
spawn_warned = set()
auto_extended_cards = set()

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

time_pattern = re.compile(
    r"(?i)\b(?:(\d{1,2}:\d{2}\s*(?:AM|PM))\s*([A-Za-z]+)|([A-Za-z]+)\s*(\d{1,2}:\d{2}\s*(?:AM|PM)))\b"
)

# ---------------- HELPERS ----------------
def get_member_timezone(member: discord.Member) -> ZoneInfo:
    for role in member.roles:
        if role.name in ROLE_TIMEZONES:
            return ZoneInfo(ROLE_TIMEZONES[role.name])
    return PHT

def parse_taken_time(time_str: str, user_tz: ZoneInfo) -> datetime:
    now_user = datetime.now(user_tz)
    try:
        parsed = datetime.strptime(time_str.strip(), "%I:%M %p")
    except ValueError:
        return None
    taken = now_user.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    if taken > now_user:
        taken -= timedelta(days=1)
    return taken.astimezone(PHT)

def calculate_next_spawn(taken_time: datetime, hours_to_add: int, minutes_to_add: int = 0) -> datetime:
    next_spawn = taken_time + timedelta(hours=hours_to_add, minutes=minutes_to_add)
    if next_spawn < taken_time:
        next_spawn += timedelta(days=1)
    return next_spawn

def unix_timestamp(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())

def build_upcoming_message():
    lines = []

    room_lines = [
        f"- {ROOM_NAMES[key]}: <t:{unix_timestamp(spawn_time)}:T> (<t:{unix_timestamp(spawn_time)}:R>)"
        for key, spawn_time in global_next_spawn.items()
        if key in ROOM_NAMES
    ]
    if room_lines:
        lines.append("🕒 **Rooms:**")
        lines.extend(room_lines)

    boss_lines = [
        f"- {BOSS_NAMES[key]}: <t:{unix_timestamp(spawn_time)}:T> (<t:{unix_timestamp(spawn_time)}:R>)"
        for key, spawn_time in global_next_spawn.items()
        if key in BOSS_NAMES
    ]
    if boss_lines:
        lines.append("🛡️ **Bosses:**")
        lines.extend(boss_lines)

    card_lines = [
        f"- {key.replace('_', ' ')}: <t:{unix_timestamp(spawn_time)}:T> (<t:{unix_timestamp(spawn_time)}:R>)"
        for key, spawn_time in global_next_spawn.items()
        if key.startswith(("PCARD", "BCARD"))
    ]
    if card_lines:
        lines.append("🃏 **Cards:**")
        lines.extend(card_lines)

    if not lines:
        lines.append("No upcoming spawns tracked.")
    return "\n".join(lines)

async def update_upcoming_message(channel: discord.TextChannel):
    global upcoming_msg_id
    content = build_upcoming_message()
    try:
        if upcoming_msg_id:
            msg = await channel.fetch_message(upcoming_msg_id)
            await msg.edit(content=content)
        else:
            msg = await channel.send(content)
            upcoming_msg_id = msg.id
    except discord.NotFound:
        msg = await channel.send(content)
        upcoming_msg_id = msg.id

# ---------------- EVENTS ----------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    cleanup_expired_messages.start()
    five_minute_warnings.start()
    card_auto_extend.start()

@tasks.loop(seconds=60)
async def cleanup_expired_messages():
    now = datetime.now(PHT)
    expired = []
    for key, spawn_time in list(global_next_spawn.items()):
        expire_time = spawn_time + (timedelta(hours=2) if key in ROOM_NAMES or key.startswith(("PCARD", "BCARD")) else timedelta(minutes=10))
        if now >= expire_time:
            expired.append(key)

    if expired:
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        text_lines = ["❌ **Expired:**"]
        for key in expired:
            spawn_str = spawn_origin_times[key].strftime("%I:%M %p")
            text_lines.append(f"- {key.replace('_', ' ')} expired (Spawned at {spawn_str})")
            del global_next_spawn[key]
            del spawn_origin_times[key]
            if key in spawn_warned:
                spawn_warned.remove(key)
            if key in auto_extended_cards:
                auto_extended_cards.remove(key)

        msg = await channel.send("\n".join(text_lines))
        await msg.delete(delay=300)  # delete after 5 min
        await update_upcoming_message(channel)

@tasks.loop(seconds=60)
async def five_minute_warnings():
    now = datetime.now(PHT)
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    for key, spawn_time in global_next_spawn.items():
        if key not in spawn_warned and 0 <= (spawn_time - now).total_seconds() <= 300:
            msg = await channel.send(f"@everyone ⚠️ {key.replace('_', ' ')} will spawn in 5 minutes!")
            await msg.delete(delay=300)  # delete after 5 min
            spawn_warned.add(key)

@tasks.loop(seconds=60)
async def card_auto_extend():
    now = datetime.now(PHT)
    for key, spawn_time in list(global_next_spawn.items()):
        if key.startswith(("PCARD", "BCARD")) and key not in auto_extended_cards:
            # Extend if card reaches its original spawn time (2h30) and hasn't been updated
            if abs((now - spawn_time).total_seconds()) <= 60:
                global_next_spawn[key] = spawn_time + timedelta(minutes=30)
                auto_extended_cards.add(key)

@bot.event
async def on_message(message):
    if message.author.bot or message.channel.id != TARGET_CHANNEL_ID:
        return

    content = message.content.strip()
    user_tz = get_member_timezone(message.author)
    user_id = message.author.id
    if user_id not in user_sent_times:
        user_sent_times[user_id] = {}

    matches = time_pattern.finditer(content)
    updated = False

    for match in matches:
        time_str = match.group(1) or match.group(4)
        key_raw = (match.group(2) or match.group(3) or "").upper()
        if not time_str or not key_raw:
            continue

        taken_time = parse_taken_time(time_str, user_tz)
        if not taken_time:
            continue

        if key_raw in BOSS_NAMES:
            next_spawn = calculate_next_spawn(taken_time, 6)
            display_key = key_raw
        elif key_raw in ROOM_NAMES:
            next_spawn = calculate_next_spawn(taken_time, 2)
            display_key = key_raw
        elif key_raw in CARD_NAMES:
            parts = content.upper().split(maxsplit=2)
            if len(parts) < 3:
                continue
            loc = parts[2]
            if loc not in CARD_LOCATIONS:
                continue
            display_key = f"{key_raw}_{CARD_LOCATIONS[loc].replace(' ', '')}"
            next_spawn = calculate_next_spawn(taken_time, 2, 30)
        else:
            continue

        spawn_origin_times[display_key] = taken_time

        if display_key in global_next_spawn and global_next_spawn[display_key] == next_spawn:
            await message.delete()
            await message.channel.send(
                f"⚠️ The next spawn for {display_key} at {next_spawn.strftime('%I:%M %p')} is already posted!",
                delete_after=6
            )
            continue

        if display_key in user_sent_times[user_id] and user_sent_times[user_id][display_key] == next_spawn:
            await message.delete()
            await message.channel.send(
                f"⚠️ You already sent the same time for {display_key}.",
                delete_after=5
            )
            continue

        user_sent_times[user_id][display_key] = next_spawn
        global_next_spawn[display_key] = next_spawn

        if display_key in auto_extended_cards:
            auto_extended_cards.remove(display_key)

        updated = True

    if updated:
        await message.delete()
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        await update_upcoming_message(channel)

    await bot.process_commands(message)

@bot.command()
async def hello(ctx):
    if ctx.channel.id == TARGET_CHANNEL_ID:
        await ctx.send("👋 Hello! I'm alive and tracking spawns here only.")

# ---------------- RUN BOT ----------------
bot.run(os.environ["TOKEN"])
