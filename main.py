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
BOSS_NAMES = {"EG": "EG Mutant", "AVG": "Avenger", "TANK": "Tank"}
CARD_NAMES = {"PCARD": "Purple Card", "BCARD": "Blue Card"}
CARD_LOCATIONS = {
    "BS UP": "Bomb Shelter Upper",
    "BS BOTTOM": "Bomb Shelter Bottom",
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
spawn_warned = {}
upcoming_msg_id = None
card_auto_extended = {}

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Time patterns
time_pattern = re.compile(
    r"(?i)\b(?:(\d{1,2}:\d{2}\s*(?:AM|PM))\s*([A-Za-z]+)|([A-Za-z]+)\s*(\d{1,2}:\d{2}\s*(?:AM|PM)))\b"
)
card_pattern = re.compile(
    r"(?i)\b(\d{1,2}:\d{2}\s*(?:AM|PM))\s+(PCARD|BCARD)\s+([A-Za-z]+)(?:\s+([A-Za-z]+))?\b"
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

def calculate_next_spawn(taken_time: datetime, hours_to_add: float) -> datetime:
    next_spawn = taken_time + timedelta(hours=hours_to_add)
    if next_spawn < taken_time:
        next_spawn += timedelta(days=1)
    return next_spawn

def unix_timestamp(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())

def build_upcoming_message():
    lines = []

    # Rooms
    room_lines = []
    for key, spawn_time in global_next_spawn.items():
        if key not in ROOM_NAMES:
            continue
        unix_ts = unix_timestamp(spawn_time)
        room_lines.append(f"- {ROOM_NAMES[key]}: <t:{unix_ts}:T> (<t:{unix_ts}:R>)")
    if room_lines:
        lines.append("🕒 **Rooms:**")
        lines.extend(room_lines)

    # Bosses
    boss_lines = []
    for key, spawn_time in global_next_spawn.items():
        if key not in BOSS_NAMES:
            continue
        unix_ts = unix_timestamp(spawn_time)
        boss_lines.append(f"- {BOSS_NAMES[key]}: <t:{unix_ts}:T> (<t:{unix_ts}:R>)")
    if boss_lines:
        lines.append("🛡️ **Bosses:**")
        lines.extend(boss_lines)

    # Cards
    card_lines = []
    for key, spawn_time in global_next_spawn.items():
        if not (key.startswith("PCARD") or key.startswith("BCARD")):
            continue
        location = key.split("_", 1)[1]
        unix_ts = unix_timestamp(spawn_time)
        card_lines.append(f"- {CARD_NAMES[key.split('_')[0]]} ({location}): <t:{unix_ts}:T> (<t:{unix_ts}:R>)")
    if card_lines:
        lines.append("🎴 **Cards:**")
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
    five_minute_warning.start()
    extend_card_time.start()

@tasks.loop(seconds=60)
async def five_minute_warning():
    now = datetime.now(PHT)
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    for key, spawn_time in list(global_next_spawn.items()):
        if key not in spawn_warned and 0 <= (spawn_time - now).total_seconds() <= 300:
            await channel.send(f"@everyone ⚠️ **{key}** will spawn in 5 minutes!")
            spawn_warned[key] = True

@tasks.loop(seconds=60)
async def cleanup_expired_messages():
    now = datetime.now(PHT)
    expired = []
    for key, spawn_time in list(global_next_spawn.items()):
        if key in ROOM_NAMES:
            expire_time = spawn_time + timedelta(hours=2)
        elif key in BOSS_NAMES:
            expire_time = spawn_time + timedelta(minutes=10)
        else:
            expire_time = spawn_time + timedelta(minutes=30)

        if now >= expire_time:
            expired.append(key)

    if expired:
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        for key in expired:
            spawn_time = global_next_spawn[key]
            await channel.send(f"❌ {key} expired (Spawned at {spawn_time.strftime('%I:%M %p')})")
            del global_next_spawn[key]
            if key in spawn_warned:
                del spawn_warned[key]
            if key in card_auto_extended:
                del card_auto_extended[key]
        await update_upcoming_message(channel)

@tasks.loop(minutes=1)
async def extend_card_time():
    now = datetime.now(PHT)
    for key, spawn_time in list(global_next_spawn.items()):
        if key.startswith("PCARD") or key.startswith("BCARD"):
            if key not in card_auto_extended:
                # Check if it's exactly 2h30 passed
                if now >= spawn_time and (now - spawn_time).total_seconds() < 60:
                    global_next_spawn[key] = spawn_time + timedelta(minutes=30)
                    card_auto_extended[key] = True

@bot.event
async def on_message(message):
    if message.author.bot or message.channel.id != TARGET_CHANNEL_ID:
        return

    user_tz = get_member_timezone(message.author)
    user_id = message.author.id
    if user_id not in user_sent_times:
        user_sent_times[user_id] = {}
    updated = False

    # Rooms & Bosses
    for match in time_pattern.finditer(message.content):
        time_str = match.group(1) or match.group(4)
        key = (match.group(2) or match.group(3) or "").upper()
        if not time_str or not key:
            continue
        taken_time = parse_taken_time(time_str, user_tz)
        if not taken_time:
            continue
        if key in BOSS_NAMES:
            next_spawn = calculate_next_spawn(taken_time, 6)
        elif key in ROOM_NAMES:
            next_spawn = calculate_next_spawn(taken_time, 2)
        else:
            continue
        if key in global_next_spawn and global_next_spawn[key] == next_spawn:
            await message.delete()
            await message.channel.send(f"⚠️ The next spawn for {key} at {next_spawn.strftime('%I:%M %p')} is already posted!", delete_after=6)
            continue
        if key in user_sent_times[user_id] and user_sent_times[user_id][key] == next_spawn:
            await message.delete()
            await message.channel.send(f"⚠️ You already sent the same time for {key}.", delete_after=5)
            continue
        user_sent_times[user_id][key] = next_spawn
        global_next_spawn[key] = next_spawn
        updated = True

    # Cards with time
    for match in card_pattern.finditer(message.content):
        time_str = match.group(1)
        card_type = match.group(2).upper()
        loc1 = match.group(3).upper()
        loc2 = (match.group(4) or "").upper()
        full_loc = f"{loc1} {loc2}".strip()
        if full_loc in CARD_LOCATIONS:
            taken_time = parse_taken_time(time_str, user_tz)
            if not taken_time:
                continue
            key = f"{card_type}_{CARD_LOCATIONS[full_loc]}"
            next_spawn = calculate_next_spawn(taken_time, 2.5)
            if key in global_next_spawn and global_next_spawn[key] == next_spawn:
                await message.delete()
                await message.channel.send(f"⚠️ The next spawn for {key} is already posted!", delete_after=6)
                continue
            if key in user_sent_times[user_id] and user_sent_times[user_id][key] == next_spawn:
                await message.delete()
                await message.channel.send(f"⚠️ You already sent the same time for {key}.", delete_after=5)
                continue
            user_sent_times[user_id][key] = next_spawn
            global_next_spawn[key] = next_spawn
            if key in card_auto_extended:
                del card_auto_extended[key]
            updated = True

    if updated:
        await message.delete()
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        await update_upcoming_message(channel)

    await bot.process_commands(message)

# ---------------- COMMANDS ----------------
@bot.command()
async def hello(ctx):
    if ctx.channel.id == TARGET_CHANNEL_ID:
        await ctx.send("👋 Hello! I'm alive and only active in this channel.")

# ---------------- RUN BOT ----------------
bot.run(os.environ["TOKEN"])
