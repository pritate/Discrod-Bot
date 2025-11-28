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

    # If the time in PHT is already past, move to next day
    if dt_pht < datetime.now(PHT):
        dt_pht += timedelta(days=1)
    return dt_pht

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

def build_embed(title: str, description: str, fields: dict, color: int = GOLD):
    embed = discord.Embed(title=title, description=description, color=color)
    for name, value in fields.items():
        embed.add_field(name=name, value=value, inline=False)
    embed.timestamp = datetime.now(tz=timezone.utc)
    return embed

def unix_ts(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())

def build_upcoming_embed(channel: discord.TextChannel):
    embed = discord.Embed(title=f"📅 Upcoming Spawns — {channel.name}", color=0x111111)
    rooms, bosses, cards = [], [], []

    now = datetime.now(PHT)
    for (cid, key), spawn_time in global_next_spawn.items():
        if cid != channel.id:
            continue
        ts = unix_ts(spawn_time)
        if key in ROOM_NAMES:
            rooms.append(f"- {ROOM_NAMES[key]} — <t:{ts}:T> (<t:{ts}:R>)")
        elif key in BOSS_NAMES:
            bosses.append(f"- {BOSS_NAMES[key]} — <t:{ts}:T> (<t:{ts}:R>)")
        elif key.startswith("PCARD") or key.startswith("BCARD"):
            parts = key.split("_", 1)
            type_k = parts[0]
            location = parts[1] if len(parts) > 1 else "Unknown"
            cards.append(f"- {CARD_NAMES.get(type_k, type_k)} ({location}) — <t:{ts}:T> (<t:{ts}:R>)")

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
            title = "⚠️ Spawn Incoming"
            desc = f"**{key.replace('_', ' ')}** will spawn soon."
            fields = {"ETA": f"<t:{unix_ts(spawn_time)}:R>"}
            embed = build_embed(title, desc, fields, color=GOLD)
            warn_msg = await channel.send(content="@everyone", embed=embed)
            spawn_warned.add((cid, key))
            asyncio.create_task(delete_later(warn_msg, 300))

@tasks.loop(seconds=60)
async def cleanup_expired_messages():
    now = datetime.now(PHT)
    expired_by_channel = {}
    to_remove = []
    for (cid, key), spawn_time in list(global_next_spawn.items()):
        if key in ROOM_NAMES:
            expire = spawn_time + timedelta(hours=2)
        elif key in BOSS_NAMES:
            expire = spawn_time + timedelta(minutes=10)
        else:
            if key.startswith("PCARD"):
                expire = spawn_time + timedelta(hours=3)
            else:
                expire = spawn_time + timedelta(hours=2, minutes=30)
        if now >= expire:
            expired_by_channel.setdefault(cid, []).append((key, spawn_origin_time.get((cid, key), spawn_time)))
            to_remove.append((cid, key))

    for cid, entries in expired_by_channel.items():
        channel = bot.get_channel(cid)
        if not channel:
            continue
        lines = []
        for key, origin in entries:
            spawned_str = origin.strftime("%I:%M %p") if origin else "Unknown"
            if key in BOSS_NAMES or key.startswith("PCARD") or key.startswith("BCARD"):
                last_spawn_record[(cid, key)] = origin or spawn_origin_time.get((cid, key))
            lines.append(f"- {key.replace('_', ' ')} (Spawned at {spawned_str})")
        embed = build_embed("❌ Expired Spawns", "The following spawns expired (no update):", {"Expired": "\n".join(lines)}, color=0xE74C3C)
        exp_msg = await channel.send(embed=embed)
        asyncio.create_task(delete_later(exp_msg, 300))

    for item in to_remove:
        cid, key = item
        global_next_spawn.pop((cid, key), None)
        spawn_warned.discard((cid, key))
        card_auto_extended.discard((cid, key))
        spawn_origin_time.pop((cid, key), None)
        ch = bot.get_channel(cid)
        if ch:
            await update_upcoming_message(ch)

@tasks.loop(minutes=1)
async def extend_card_time():
    now = datetime.now(PHT)
    for (cid, key), spawn_time in list(global_next_spawn.items()):
        if key.startswith("BCARD"):
            if (cid, key) in card_auto_extended:
                continue
            if now >= spawn_time and (now - spawn_time).total_seconds() < 120:
                global_next_spawn[(cid, key)] = spawn_time + timedelta(minutes=30)
                card_auto_extended.add((cid, key))
                ch = bot.get_channel(cid)
                if ch:
                    await update_upcoming_message(ch)

async def delete_later(msg: discord.Message, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    try:
        await msg.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

# ---------------- COMMANDS ----------------
@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear_cmd(ctx, amount: int = 20):
    if ctx.channel.id not in ALLOWED_CHANNELS:
        return
    await ctx.channel.purge(limit=amount + 1)
    notice = await ctx.send(f"Cleared last {amount} messages.")
    await asyncio.sleep(5)
    await notice.delete()

@clear_cmd.error
async def clear_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need Manage Messages permission to use this command.", delete_after=6)

@bot.command(name="help")
async def help_cmd(ctx):
    if ctx.channel.id not in ALLOWED_CHANNELS:
        return
    rooms_list = ", ".join([f"{k} ({v})" for k, v in ROOM_NAMES.items()])
    cards_list = ", ".join([f"{k} ({v})" for k, v in CARD_NAMES.items()])
    bosses_list = ", ".join([f"{k} ({v})" for k, v in BOSS_NAMES.items()])

    embed = discord.Embed(
        title="🧭 Command Help — Timer Tracker Bot",
        description="Here's a quick guide on how to use the bot effectively.",
        color=GOLD
    )
    embed.add_field(
        name="🎴 Accepted Keywords",
        value=(
            f"**Cards:** {cards_list}\n"
            f"**Rooms:** {rooms_list}\n"
            f"**Bosses:** {bosses_list}"
        ),
        inline=False
    )
    embed.add_field(
        name="💬 Flexible Input Format",
        value=(
            "You can send inputs in **any order**. Examples:\n"
            "`pcard nuc 12:30am`\n"
            "`12:30am pcard nuc`\n"
            "`pcard rb 1:15am`\n"
            "`bcard crude 2:00pm`\n"
            "`eg 3:30pm`\n"
            "`bn 4:00pm`"
        ),
        inline=False
    )
    embed.add_field(
        name="⏱️ Timers",
        value=(
            "• **PCARD (Purple Card)** — fixed **3 hours**.\n"
            "• **BCARD (Blue Card)** — **2.5 hours** (auto-extends +30m if not updated).\n"
            "• **Rooms** — **2 hours**.\n"
            "• **Bosses** — **6 hours**, except **Bloodnest (BN)** — **3 hours**."
        ),
        inline=False
    )
    embed.add_field(
        name="🌍 Timezone Support",
        value="The bot reads your timezone role automatically (PH, IND, MY, RU, US, TH, AU). If no role, defaults to PH.",
        inline=False
    )
    embed.add_field(
        name="🧹 Clear Command",
        value="`!clear <amount>` — Deletes recent messages (default: 20). Requires Manage Messages permission.",
        inline=False
    )
    embed.set_footer(text="Timers auto-update and confirmation messages auto-delete.")
    await ctx.send(embed=embed)

# ---------------- MESSAGE HANDLER ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.channel.id not in ALLOWED_CHANNELS:
        return

    channel_id = message.channel.id
    user_tz = get_member_timezone(message.author)
    user_id = message.author.id

    time_str, category, location = find_time_keyword_location(message.content)
    if not category:
        await bot.process_commands(message)
        return

    # Determine spawn key
    if category in ROOM_NAMES:
        spawn_key = category
    elif category in BOSS_NAMES:
        spawn_key = category
    elif category.startswith("PCARD") or category.startswith("BCARD"):
        spawn_key = f"{category}_{location}" if location else f"{category}_UNKNOWN"
    else:
        spawn_key = category

    # Determine taken time
    taken_time_pht = None
    if time_str:
        taken_time_pht = parse_time_string_to_pht(time_str, user_tz)
    if not taken_time_pht:
        now_user = datetime.now(user_tz)
        taken_time_pht = now_user.astimezone(PHT)

    # Determine duration
    if spawn_key in ROOM_NAMES:
        duration_hours = 2
    elif spawn_key in BOSS_NAMES:
        duration_hours = 6 if spawn_key != "BN" else 3
    elif spawn_key.startswith("PCARD"):
        duration_hours = 3
    elif spawn_key.startswith("BCARD"):
        duration_hours = 2.5
    else:
        duration_hours = 2

    # Calculate next spawn
    next_spawn = taken_time_pht + timedelta(hours=duration_hours)
    # Ensure next_spawn is not in the past
    if next_spawn < datetime.now(PHT):
        next_spawn += timedelta(days=1)

    global_next_spawn[(channel_id, spawn_key)] = next_spawn
    spawn_origin_time[(channel_id, spawn_key)] = taken_time_pht
    last_spawn_record[(channel_id, spawn_key)] = taken_time_pht

    embed = build_embed(
        "✅ Timer Updated",
        f"{spawn_key.replace('_',' ')} registered by {message.author.display_name}",
        {"Next Spawn": f"<t:{unix_ts(next_spawn)}:T> (<t:{unix_ts(next_spawn)}:R>)"},
    )
    confirmation_msg = await message.channel.send(embed=embed)
    asyncio.create_task(delete_later(confirmation_msg, 20))

    await update_upcoming_message(message.channel)
    await bot.process_commands(message)

# ---------------- RUN BOT ----------------
bot.run(os.environ["TOKEN"])
