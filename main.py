import discord
from discord.ext import commands, tasks
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
from keep_alive import keep_alive

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
upcoming_msg_id = None

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Time pattern: HH:MM AM/PM + LABEL
time_pattern = re.compile(
    r"(?i)\b(?:(\d{1,2}:\d{2}\s*(?:AM|PM))\s*([A-Za-z]+)|([A-Za-z]+)\s*(\d{1,2}:\d{2}\s*(?:AM|PM)))\b"
)

# ---------------- HELPERS ----------------
def get_member_timezone(member: discord.Member) -> ZoneInfo:
    for role in member.roles:
        if role.name in ROLE_TIMEZONES:
            return ZoneInfo(ROLE_TIMEZONES[role.name])
    return PHT  # default to PHT

def parse_taken_time(time_str: str, user_tz: ZoneInfo) -> datetime:
    now_user = datetime.now(user_tz)
    try:
        parsed = datetime.strptime(time_str.strip(), "%I:%M %p")
    except ValueError:
        return None

    # Use today's date but user's timezone
    taken = now_user.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)

    # If input time is in the future (e.g., user types 11 PM but it's already 11:30 PM),
    # assume they mean the previous day to keep spawn calculations accurate.
    if taken > now_user:
        taken -= timedelta(days=1)

    # Convert user local time to PHT for uniform spawn tracking
    taken_pht = taken.astimezone(PHT)
    return taken_pht

def calculate_next_spawn(taken_time: datetime, hours_to_add: int) -> datetime:
    next_spawn = taken_time + timedelta(hours=hours_to_add)
    # If the result ends up being before the taken time, add 1 day (handles midnight crossover)
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
        room_name = ROOM_NAMES[key]
        room_lines.append(f"- {room_name}: <t:{unix_ts}:T> (<t:{unix_ts}:R>)")
    if room_lines:
        lines.append("🕒 **Rooms:**")
        lines.extend(room_lines)

    # Bosses
    boss_lines = []
    for key, spawn_time in global_next_spawn.items():
        if key not in BOSS_NAMES:
            continue
        unix_ts = unix_timestamp(spawn_time)
        boss_name = BOSS_NAMES[key]
        boss_lines.append(f"- {boss_name}: <t:{unix_ts}:T> (<t:{unix_ts}:R>)")
    if boss_lines:
        lines.append("🛡️ **Bosses:**")
        lines.extend(boss_lines)

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

@tasks.loop(seconds=60)
async def cleanup_expired_messages():
    now = datetime.now(PHT)
    expired = []
    for key, spawn_time in global_next_spawn.items():
        expire_time = spawn_time + (timedelta(hours=2) if key in ROOM_NAMES else timedelta(minutes=10))
        if now >= expire_time:
            expired.append(key)
    for key in expired:
        del global_next_spawn[key]

    if expired:
        channel = bot.get_channel(TARGET_CHANNEL_ID)
        if channel:
            await update_upcoming_message(channel)

@bot.event
async def on_message(message):
    if message.author.bot or message.channel.id != TARGET_CHANNEL_ID:
        return

    matches = time_pattern.finditer(message.content)
    user_tz = get_member_timezone(message.author)
    user_id = message.author.id

    if user_id not in user_sent_times:
        user_sent_times[user_id] = {}

    updated = False

    for match in matches:
        time_str = match.group(1) or match.group(4)
        key = (match.group(2) or match.group(3) or "").upper()
        if not time_str or not key:
            continue

        taken_time = parse_taken_time(time_str, user_tz)
        if not taken_time:
            continue

        # Determine spawn time based on type
        if key in BOSS_NAMES:
            next_spawn = calculate_next_spawn(taken_time, 6)
        elif key in ROOM_NAMES:
            next_spawn = calculate_next_spawn(taken_time, 2)
        else:
            continue

        # Global duplicate check
        if key in global_next_spawn and global_next_spawn[key] == next_spawn:
            await message.delete()
            await message.channel.send(
                f"⚠️ The next spawn for {key} at {next_spawn.strftime('%I:%M %p')} is already posted!",
                delete_after=6
            )
            continue

        # Per-user duplicate check
        if key in user_sent_times[user_id] and user_sent_times[user_id][key] == next_spawn:
            await message.delete()
            await message.channel.send(f"⚠️ You already sent the same time for {key}.", delete_after=5)
            continue

        # Save new time
        user_sent_times[user_id][key] = next_spawn
        global_next_spawn[key] = next_spawn
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
keep_alive()
bot.run(os.environ["TOKEN"])