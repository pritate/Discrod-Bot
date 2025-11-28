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
    # new locations
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
    "BN": "Bloodnest"  # new boss
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
    # new locations (same keys as ROOM_NAMES where applicable)
    "RB": "Rocket Base",
    "CRUDE": "Crude Oil Base",
    "BS SNOW": "Snow Mountain Bomb Shelter",
    "DOCK": "Dock",
    "FACTORY": "Chemical Factory",
}

# timezone role mapping (role names must match these keys, case-insensitive)
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
user_sent_times = {}        # per-user posted times (by user_id -> key -> datetime)
global_next_spawn = {}      # key: (channel_id, spawn_key) -> datetime (PHT)
spawn_warned = set()        # set of (channel_id, spawn_key) that were warned
upcoming_msg_id = {}        # channel_id -> message id for the tracker embed
card_auto_extended = set()  # (channel_id, spawn_key) that were auto-extended
spawn_origin_time = {}      # (channel_id, spawn_key) -> original taken time (PHT)
last_spawn_record = {}      # (channel_id, spawn_key) -> last taken time for bosses/cards

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ---------------- REGEX / PARSING ----------------
# Time pattern requires AM/PM (flexible spacing/case)
time_regex = re.compile(r"(?i)(\d{1,2}:\d{2}\s*(?:AM|PM))")
# tokens regex for words (for matching locations / keywords)
word_token = re.compile(r"([A-Za-z]+)")

# synonyms / aliases map for locations and keywords (all uppercase for normalization)
LOCATION_ALIASES = {
    # Bomb shelter bottom should accept bs + bottom synonyms (user requested)
    "BS": "BS BOT",        # default 'bs' => bottom (changed from previous BS UP)
    "BSUP": "BS UP",
    "BSUPPER": "BS UP",
    "BS BOT": "BS BOT",
    "BSBOT": "BS BOT",
    "BOT": "BS BOT",
    "BOTTOM": "BS BOT",
    "DOWN": "BS BOT",
    "BELOW": "BS BOT",
    # standard locations
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
    # new aliases / locations
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
    # small harbor: accept only SHB and its uppercase normalized token
    "SHB": "SHB",
    # NOTE: do NOT map "SMALLHARBOR" by user request — they only want SHB accepted
}

# allowed keywords for boss or room detection (uppercase)
ROOM_KEYS = set(ROOM_NAMES.keys()) | {v.upper() for v in ROOM_NAMES.values()}
BOSS_KEYS = set(BOSS_NAMES.keys()) | {v.upper() for v in BOSS_NAMES.values()}
CARD_KEYS = set(CARD_NAMES.keys()) | {v.upper() for v in CARD_NAMES.values()}

# ---------------- HELPERS ----------------
def get_member_timezone(member: discord.Member) -> ZoneInfo:
    # check member roles for one matching ROLE_TIMEZONES (case-insensitive)
    for role in member.roles:
        key = role.name.upper()
        if key in ROLE_TIMEZONES:
            return ZoneInfo(ROLE_TIMEZONES[key])
    return PHT

def parse_time_string_to_pht(time_str: str, user_tz: ZoneInfo) -> datetime | None:
    """Parse a time string with AM/PM in user's timezone and return a timezone-aware dt in PHT."""
    try:
        parsed = datetime.strptime(time_str.strip().upper(), "%I:%M %p")
    except ValueError:
        return None
    now_user = datetime.now(user_tz)
    # place parsed time on today's date in user tz
    dt_user = now_user.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    # if input is in the future (later today), assume they meant earlier (taken) -> treat as previous day
    if dt_user > now_user:
        dt_user -= timedelta(days=1)
    dt_pht = dt_user.astimezone(PHT)
    return dt_pht

def calculate_next_spawn(taken_time: datetime, hours: float) -> datetime:
    # hours may be fractional (2.5)
    next_spawn = taken_time + timedelta(hours=hours)
    if next_spawn < taken_time:
        next_spawn += timedelta(days=1)
    return next_spawn

def unix_ts(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())

def normalize_token(tok: str) -> str:
    return re.sub(r'[^A-Za-z]', '', tok).upper()

def find_time_keyword_location(message: str):
    """
    Flexible parser: extract (time_str or None), (type_key or None), (location_key or None)
    Accepts any order. Returns tuple (time_str, type_key, location_key)
    - time_str: matched 'HH:MM AM/PM' (string)
    - type_key: one of CARD_KEYS or BOSS_KEYS or ROOM_KEYS (string)
    - location_key: normalized key used for lookup (e.g. 'NUC' or 'BS BOT')
    """
    s = message
    # find time
    time_m = time_regex.search(s)
    time_str = time_m.group(1).upper() if time_m else None

    # tokens (words)
    tokens = [normalize_token(t) for t in word_token.findall(s)]
    type_key = None
    location_key = None

    # look for card keywords first (PCARD / BCARD)
    for t in tokens:
        if t in CARD_KEYS:
            type_key = t
            break

    # look for boss keywords
    if not type_key:
        for t in tokens:
            if t in BOSS_KEYS:
                type_key = t
                break

    # special boss alias: accept "BLOOD" as BN (user requested)
    if not type_key and "BLOOD" in tokens:
        type_key = "BN"

    # look for room keys
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

    # normalize type_key: map full names to short codes where possible
    if type_key:
        tk = type_key
        rev_room = {v.upper(): k for k, v in ROOM_NAMES.items()}
        rev_boss = {v.upper(): k for k, v in BOSS_NAMES.items()}
        if tk in rev_room:
            type_key = rev_room[tk]
        elif tk in rev_boss:
            type_key = rev_boss[tk]
        # else leave as-is (caller checks membership)

    return time_str, type_key, location_key

# ---------------- EMBED HELPERS (dark/gold theme) ----------------
GOLD = 0xD4AF37
DARK = 0x0B0B0B

def build_embed(title: str, description: str, fields: dict, color: int = GOLD):
    embed = discord.Embed(title=title, description=description, color=color)
    for name, value in fields.items():
        embed.add_field(name=name, value=value, inline=False)
    embed.timestamp = datetime.now(tz=timezone.utc)
    return embed

def build_upcoming_embed(channel: discord.TextChannel):
    embed = discord.Embed(title=f"📅 Upcoming Spawns — {channel.name}", color=0x111111)
    rooms = []
    bosses = []
    cards = []
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

    # If nothing upcoming, show last spawns for bosses/cards if available
    if not rooms and not bosses and not cards:
        lines = []
        for (cid, key), last_taken in last_spawn_record.items():
            if cid != channel.id:
                continue
            # only include boss/card last spawns
            if key in BOSS_NAMES or key.startswith("PCARD") or key.startswith("BCARD"):
                tstr = last_taken.strftime("%I:%M %p") if last_taken else "Unknown"
                label = (BOSS_NAMES.get(key) or CARD_NAMES.get(key.split("_", 1)[0], key))
                lines.append(f"- {label} ({key.replace('_',' ')}) — Last Spawned at {tstr}")
        if lines:
            embed.add_field(name="🕰️ Last Spawns (expired)", value="\n".join(lines), inline=False)
        else:
            embed.description = "No upcoming spawns tracked."
    return embed

async def update_upcoming_message(channel: discord.TextChannel):
    embed = build_upcoming_embed(channel)
    # send/edit persistent embed per channel
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
    expired_by_channel = {}  # cid -> list of (key, spawned_at)
    to_remove = []
    for (cid, key), spawn_time in list(global_next_spawn.items()):
        # expiration intervals:
        # rooms 2h, bosses 10m, cards: BCARD 2h30 (with extension), PCARD 3h (fixed)
        if key in ROOM_NAMES:
            expire = spawn_time + timedelta(hours=2)
        elif key in BOSS_NAMES:
            expire = spawn_time + timedelta(minutes=10)
        else:
            # card: determine by prefix
            if key.startswith("PCARD"):
                expire = spawn_time + timedelta(hours=3)
            else:
                # BCARD default expiration before extension
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
            # For bosses and cards we keep the last taken time in last_spawn_record for tracking
            if key in BOSS_NAMES or key.startswith("PCARD") or key.startswith("BCARD"):
                last_spawn_record[(cid, key)] = origin or spawn_time
            lines.append(f"- {key.replace('_', ' ')} (Spawned at {spawned_str})")
        embed = build_embed("❌ Expired Spawns", "The following spawns expired (no update):", {"Expired": "\n".join(lines)}, color=0xE74C3C)
        exp_msg = await channel.send(embed=embed)
        asyncio.create_task(delete_later(exp_msg, 300))

    # cleanup data store (remove expired entries from active tracking)
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
    """
    Auto-extend behavior:
    - PCARD is fixed at 3 hours -> no auto-extension.
    - BCARD: if it reaches its first spawn (2h30) and not updated, extend by +30min.
    """
    now = datetime.now(PHT)
    for (cid, key), spawn_time in list(global_next_spawn.items()):
        # only BCARD auto-extend (blue card)
        if key.startswith("BCARD"):
            if (cid, key) in card_auto_extended:
                continue
            # if now is at or just after the spawn_time (the moment 2h30 arrived)
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

# ---------------- CLEAR COMMAND ----------------
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

# ---------------- HELP COMMAND ----------------
@bot.command(name="help")
async def help_cmd(ctx):
    if ctx.channel.id not in ALLOWED_CHANNELS:
        return
    # build list strings from current mappings (so it stays accurate)
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
            "`bn 4:00pm` (bloodnest)\n"
            "The bot will detect the type, location, and time automatically."
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
    embed.set_footer(text="Timers auto-update and the confirmation messages auto-delete to keep channels clean.")
    await ctx.send(embed=embed)

# ---------------- MESSAGE HANDLER ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.channel.id not in ALLOWED_CHANNELS:
        return

    channel_id = message.channel.id
    user_tz = get_member_timezone(message.author)
    user_id = message.author.id
    user_sent_times.setdefault(user_id, {})
    updated = False

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

    # Now we need time_str to compute taken_time (PHT)
    taken_time_pht = None
    if time_str:
        taken_time_pht = parse_time_string_to_pht(time_str, user_tz)
    # If time missing but category present, assume taken_time = now in user's tz
    if not taken_time_pht and category in ("card", "room", "boss"):
        now_user = datetime.now(user_tz)
        taken_time_pht = now_user.astimezone(PHT)

    # If still no category recognized, bail
    if not category:
        await bot.process_commands(message)
        return

    # Build internal key name
    if category == "boss":
        spawn_key = key if key else "UNKNOWN_BOSS"
    elif category == "room":
        spawn_key = key if key else (next((k for k, v in ROOM_NAMES.items() if v == location_for_display), "UNKNOWN_ROOM"))
    else:  # card
        card_short = card_type if 'card_type' in locals() else "PCARD"
        loc_label = (location_for_display or "Unknown").replace(" ", "")
        spawn_key = f"{card_short}_{loc_label}"

    # Determine next_spawn based on rules
    if category == "boss":
        # special: Bloodnest BN -> 3 hours, otherwise 6 hours
        if spawn_key == "BN":
            next_spawn = calculate_next_spawn(taken_time_pht, 3)
        else:
            next_spawn = calculate_next_spawn(taken_time_pht, 6)
    elif category == "room":
        next_spawn = calculate_next_spawn(taken_time_pht, 2)
    else:  # card
        # PCARD fixed to 3 hours, BCARD remains 2.5 (and BCARD may auto-extend)
        if spawn_key.startswith("PCARD"):
            next_spawn = calculate_next_spawn(taken_time_pht, 3)
        else:
            next_spawn = calculate_next_spawn(taken_time_pht, 2.5)

    # Use channel-scoped key
    channel_key = (channel_id, spawn_key)

    # Duplicate checks: channel-scoped global and per-user
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

    # Save times
    user_sent_times.setdefault(user_id, {})[spawn_key] = next_spawn
    global_next_spawn[channel_key] = next_spawn
    spawn_origin_time[channel_key] = taken_time_pht
    # ensure card auto-extend flag reset
    card_auto_extended.discard(channel_key)
    spawn_warned.discard(channel_key)
    # clear last_spawn_record for this spawn because it has been updated
    last_spawn_record.pop((channel_id, spawn_key), None)

    # send confirmation embed (auto-delete after 5m)
    desc = f"{spawn_key.replace('_',' ')}"
    fields = {
        "Spawned At": taken_time_pht.strftime("%I:%M %p"),
        "Next Spawn": f"<t:{unix_ts(next_spawn)}:T> (<t:{unix_ts(next_spawn)}:R>)"
    }
    if location_for_display:
        fields["Location"] = location_for_display
    confirm_embed = build_embed("✅ Timer Set", desc, fields, color=GOLD)
    confirm_msg = await message.channel.send(embed=confirm_embed)
    asyncio.create_task(delete_later(confirm_msg, 300))

    updated = True
    if updated:
        try:
            await message.delete()
        except:
            pass
        await update_upcoming_message(message.channel)

    await bot.process_commands(message)

# ---------------- FALLBACK COMMANDS ----------------
@bot.command()
async def hello(ctx):
    if ctx.channel.id in ALLOWED_CHANNELS:
        await ctx.send("👋 Hello! I'm alive and active in this channel.")

# ---------------- RUN ----------------
bot.run(os.environ["TOKEN"])
