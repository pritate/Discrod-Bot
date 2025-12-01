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

# ---------------- CARD LOCATIONS (Added ARC / AFC for blue cards) ----------------
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
    # NEW blue-card-only locations:
    "ARC": "Abandoned Research Center",   # arc
    "AFC": "Abandoned Factory Center",    # afc
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
global_next_spawn = {}      # (channel_id, spawn_key) -> datetime (PHT)
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

# LOCATION_ALIASES now includes ARC and AFC (these are card locations only in CARD_LOCATIONS)
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
    # blue-card-only aliases:
    "ARC": "ARC",
    "AFC": "AFC",
}

ROOM_KEYS = set(ROOM_NAMES.keys()) | {v.upper() for v in ROOM_NAMES.values()}
BOSS_KEYS = set(BOSS_NAMES.keys()) | {v.upper() for v in BOSS_NAMES.values()}
CARD_KEYS = set(CARD_NAMES.keys()) | {v.upper() for v in CARD_NAMES.values()}

# ---------------- HELPERS ----------------
def get_member_timezone(member: discord.Member) -> ZoneInfo:
    # Return ZoneInfo based on user role; default to PHT
    for role in member.roles:
        key = role.name.upper()
        if key in ROLE_TIMEZONES:
            return ZoneInfo(ROLE_TIMEZONES[key])
    return PHT

def parse_time_string_to_pht(time_str: str, user_tz: ZoneInfo) -> datetime | None:
    """
    Parse a user-provided time (like "6:13 PM") as a datetime in PHT representing
    when the spawn/taken-time happened.

    Important behavior (fix for 'in 27h' bug):
    - Construct dt_user on user's 'today'.
    - If that dt_user is in the future relative to now in user's tz, assume the user meant the previous day (i.e., taken earlier),
      so subtract one day (this avoids moving a past taken-time forward into the future).
    - Only after we compute next_spawn we will move next_spawn forward if needed.
    """
    try:
        parsed = datetime.strptime(time_str.strip().upper(), "%I:%M %p")
    except ValueError:
        return None

    now_user = datetime.now(user_tz)
    # place parsed time on today's date in user's timezone
    dt_user = now_user.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)

    # If the parsed time is in the future relative to the user's now, assume it was earlier (yesterday)
    if dt_user > now_user:
        dt_user -= timedelta(days=1)

    # convert to PHT and return
    dt_pht = dt_user.astimezone(PHT)
    return dt_pht

def normalize_token(tok: str) -> str:
    return re.sub(r'[^A-Za-z]', '', tok).upper()

def find_time_keyword_location(message: str):
    """
    Extract (time_str, type_key, location_key) from message text.
    - time_str is the raw 'HH:MM AM/PM' substring (or None).
    - type_key is a detected card/boss/room key (like PCARD, EG, AP, etc.) or None.
    - location_key is a normalized alias key (like 'NUC', 'BS BOT', 'ARC', etc.) or None.
    """
    s = message
    time_m = time_regex.search(s)
    time_str = time_m.group(1).upper() if time_m else None

    tokens = [normalize_token(t) for t in word_token.findall(s)]
    type_key = None
    location_key = None

    # priority: cards -> bosses -> rooms
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

    # find location token(s) (try pairs first for things like "BS BOT" or "BS SNOW")
    for i in range(len(tokens)-1, -1, -1):
        two = (tokens[i-1] + " " + tokens[i]) if i-1 >= 0 else tokens[i]
        if two in LOCATION_ALIASES:
            location_key = LOCATION_ALIASES[two]
            break
        if tokens[i] in LOCATION_ALIASES:
            location_key = LOCATION_ALIASES[tokens[i]]
            break

    # map full names back to short codes if needed
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
            # if it's ARC/AFC or other label, display friendly name where available
            location_label = CARD_LOCATIONS.get(location, location)
            cards.append(f"- {CARD_NAMES.get(type_k, type_k)} ({location_label}) — <t:{ts}:T> (<t:{ts}:R>)")

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
            # when the original BCARD spawn_time arrives, auto-extend by 30m once
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
    await ctx.channel.purge(limit=amount + 1)  # +1 to include the command message
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

    # flexible parse
    time_str, type_key, loc_key = find_time_keyword_location(message.content)

    # If nothing detected, just ignore and process commands
    if not time_str and not type_key and not loc_key:
        await bot.process_commands(message)
        return

    # Try to determine category and location properly
    category = None
    key = None
    location_for_display = None

    if type_key and type_key in CARD_KEYS:
        category = "card"
        card_type = type_key  # PCARD/BCARD
    elif type_key and type_key in BOSS_KEYS:
        category = "boss"
        if type_key in BOSS_NAMES:
            key = type_key
        else:
            rev = {v.upper(): k for k, v in BOSS_NAMES.items()}
            key = rev.get(type_key, type_key)
    elif type_key and type_key in ROOM_KEYS:
        category = "room"
        if type_key in ROOM_NAMES:
            key = type_key
        else:
            rev = {v.upper(): k for k, v in ROOM_NAMES.items()}
            key = rev.get(type_key, type_key)

    # If no type_key but we have location that matches a room, assume room
    if not category and loc_key:
        loc_norm = loc_key.upper()
        for short, name in ROOM_NAMES.items():
            NAME_ALT = {short, name.upper()}
            if loc_norm == short or loc_norm == name.upper() or loc_norm in NAME_ALT:
                category = "room"
                key = short
                break

    # If still no category but tokens include boss/room/card names, try that
    if not category:
        for t in re.findall(r"[A-Za-z]+", message.content):
            T = t.upper()
            if T in BOSS_NAMES:
                category = "boss"
                key = T
                break
            if T in ROOM_NAMES:
                category = "room"
                key = T
                break
            if T in CARD_NAMES:
                category = "card"
                card_type = T
                break
            # accept 'BLOOD' -> BN
            if T == "BLOOD":
                category = "boss"
                key = "BN"
                break

    # Determine location_for_display for cards and rooms
    if loc_key:
        L = loc_key.upper()
        if L in LOCATION_ALIASES:
            mapped = LOCATION_ALIASES[L]
            if mapped in CARD_LOCATIONS:
                location_for_display = CARD_LOCATIONS[mapped]
            elif mapped in ROOM_NAMES:
                location_for_display = ROOM_NAMES[mapped]
            else:
                location_for_display = CARD_LOCATIONS.get(mapped, mapped)
        else:
            if L in CARD_LOCATIONS:
                location_for_display = CARD_LOCATIONS[L]
            elif L in ROOM_NAMES:
                location_for_display = ROOM_NAMES[L]
            else:
                location_for_display = CARD_LOCATIONS.get(L) or ROOM_NAMES.get(L)

    # If category is card but no explicit location found, attempt to find token that matches location names
    if category == "card" and not location_for_display:
        for tok in re.findall(r"[A-Za-z]+", message.content):
            T = tok.upper()
            if T in LOCATION_ALIASES:
                mapped = LOCATION_ALIASES[T]
                location_for_display = CARD_LOCATIONS.get(mapped, CARD_LOCATIONS.get(mapped))
                break
            if T in CARD_LOCATIONS:
                location_for_display = CARD_LOCATIONS[T]
                break

    # Determine taken time (converted to PHT). If parse fails, fallback to now in user's tz -> PHT
    taken_time_pht = None
    if time_str:
        taken_time_pht = parse_time_string_to_pht(time_str, user_tz)
    if not taken_time_pht:
        now_user = datetime.now(user_tz)
        taken_time_pht = now_user.astimezone(PHT)

    # Build internal key name
    if category == "boss":
        spawn_key = key if key else "UNKNOWN_BOSS"
    elif category == "room":
        spawn_key = key if key else (next((k for k, v in ROOM_NAMES.items() if v == location_for_display), "UNKNOWN_ROOM"))
    else:  # card
        card_short = card_type if 'card_type' in locals() else "PCARD"
        # prefer alias (ARC/AFC etc.) if available for spawn_key label
        loc_label = None
        # find mapping key for location_for_display if possible
        if location_for_display:
            # reverse map CARD_LOCATIONS to find key
            rev_card_locs = {v.upper(): k for k, v in CARD_LOCATIONS.items()}
            loc_label = rev_card_locs.get(location_for_display.upper(), None)
        # if loc_key present and is normalized alias, use that
        if loc_key:
            loc_label = LOCATION_ALIASES.get(loc_key.upper(), loc_label)
        loc_label = (loc_label or "UNKNOWN").replace(" ", "")
        spawn_key = f"{card_short}_{loc_label}"

    # Determine duration_hours
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

    # Calculate next spawn from the taken_time. If next_spawn is still in the past, move it forward by 1 day.
    now_pht = datetime.now(PHT)
    next_spawn = taken_time_pht + timedelta(hours=duration_hours)
    if next_spawn < now_pht:
        # This usually means the next spawn will be tomorrow at that time + duration
        next_spawn += timedelta(days=1)

    # duplicate checks (channel-scoped)
    channel_key = (channel_id, spawn_key)
    if channel_key in global_next_spawn and global_next_spawn[channel_key] == next_spawn:
        try:
            await message.delete()
        except:
            pass
        await message.channel.send(f"⚠️ The next spawn for {spawn_key} at {next_spawn.strftime('%I:%M %p')} is already posted!", delete_after=6)
        return

    if spawn_key in user_sent_times.get(user_id, {}) and user_sent_times[user_id][spawn_key] == next_spawn:
        try:
            await message.delete()
        except:
            pass
        await message.channel.send(f"⚠️ You already sent the same time for {spawn_key}.", delete_after=5)
        return

    # Save times and update trackers
    user_sent_times.setdefault(user_id, {})[spawn_key] = next_spawn
    global_next_spawn[channel_key] = next_spawn
    spawn_origin_time[channel_key] = taken_time_pht
    card_auto_extended.discard(channel_key)
    spawn_warned.discard(channel_key)
    last_spawn_record.pop((channel_id, spawn_key), None)

    # send confirmation embed (auto-delete after 5m)
    desc = f"{spawn_key.replace('_',' ')}"
    fields = {
        "Spawned At": taken_time_pht.strftime("%I:%M %p"),
        "Next Spawn": f"<t:{unix_ts(next_spawn)}:T> (<t:{unix_ts(next_spawn)}:R>)"
    }
    # include friendly location if available
    if location_for_display:
        fields["Location"] = location_for_display
    confirm_embed = build_embed("✅ Timer Set", desc, fields, color=GOLD)
    confirm_msg = await message.channel.send(embed=confirm_embed)
    asyncio.create_task(delete_later(confirm_msg, 300))

    # update upcoming embed
    try:
        await message.delete()
    except:
        pass
    await update_upcoming_message(message.channel)
    await bot.process_commands(message)

# ---------------- RUN BOT ----------------
bot.run(os.environ["TOKEN"])
