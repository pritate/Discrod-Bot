import discord
from discord.ext import commands, tasks
import re
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os

# ---------------- CONFIG ----------------
ALLOWED_CHANNELS = [1425720821477015553, 1427263126989963264]
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
    "SHB": "Small Harbor",          # FIX: was missing from CARD_LOCATIONS
    "NUC": "Nuclear Plant",
    "MILI": "Military Base",
    "BIO": "Bio-Research Lab",
    "BANDIT": "Bandit Camp",
    "RB": "Rocket Base",
    "CRUDE": "Crude Oil Base",
    "BS SNOW": "Snow Mountain Bomb Shelter",
    "DOCK": "Dock",
    "FACTORY": "Chemical Factory",
    "ARC": "Abandoned Research Center",
    "AFC": "Abandoned Factory Center",
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
user_sent_times = {}
global_next_spawn = {}      # (channel_id, spawn_key) -> datetime (PHT) of NEXT spawn
spawn_warned = set()
upcoming_msg_id = {}
card_auto_extended = set()
spawn_origin_time = {}      # original taken time (PHT)
last_spawn_record = {}

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
    "SMALLHARBOR": "SHB",
    "ARC": "ARC",
    "AFC": "AFC",
}

ROOM_KEYS = set(ROOM_NAMES.keys()) | {v.upper() for v in ROOM_NAMES.values()}
BOSS_KEYS = set(BOSS_NAMES.keys()) | {v.upper() for v in BOSS_NAMES.values()}
CARD_KEYS = set(CARD_NAMES.keys()) | {v.upper() for v in CARD_NAMES.values()}


# ---------------- DURATION HELPER ----------------
def get_duration_hours(spawn_key: str) -> float:
    """Return the respawn duration in hours for a given spawn_key."""
    if spawn_key in ROOM_NAMES:
        return 2.0
    if spawn_key == "BN":
        return 3.0
    if spawn_key in BOSS_NAMES:
        return 6.0
    if spawn_key.startswith("PCARD"):
        return 3.0
    if spawn_key.startswith("BCARD"):
        return 2.5
    # Fallback — log unexpected keys so they're visible
    print(f"[WARN] Unknown spawn_key '{spawn_key}', defaulting duration to 2h")
    return 2.0


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
    dt_user = datetime(
        year=now_user.year,
        month=now_user.month,
        day=now_user.day,
        hour=parsed.hour,
        minute=parsed.minute,
        tzinfo=user_tz
    )
    if dt_user > now_user + timedelta(hours=12):
        dt_user -= timedelta(days=1)

    return dt_user.astimezone(PHT)

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

    for (cid, key), spawn_time in list(global_next_spawn.items()):
        if cid != channel.id:
            continue

        # FIX: correctly compute expire_time per category
        duration = get_duration_hours(key)
        # spawn_time here is the NEXT spawn time (already offset from taken time)
        # The entry expires when that spawn window closes (spawn_time + a grace period)
        expire_time = spawn_time + timedelta(minutes=10)

        if now >= expire_time:
            continue

        taken = spawn_origin_time.get((cid, key))
        taken_str = taken.strftime("%I:%M %p") if taken else "?"
        spawn_str = spawn_time.strftime("%I:%M %p").lstrip("0")
        line = f"**{key.replace('_', ' ')}** — spawns <t:{unix_ts(spawn_time)}:t> (spawns at {spawn_str} PHT)"

        if key in ROOM_NAMES:
            rooms.append(line)
        elif key in BOSS_NAMES:
            bosses.append(line)
        else:
            cards.append(line)

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
        # FIX: skip if the entry no longer exists (race with cleanup)
        if (cid, key) not in global_next_spawn:
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
        # FIX: apply the correct duration offsets per category
        duration = get_duration_hours(key)
        expire = spawn_time + timedelta(minutes=10)  # grace period after spawn window opens

        if now >= expire:
            expired_by_channel.setdefault(cid, []).append(
                (key, spawn_origin_time.get((cid, key), spawn_time))
            )
            to_remove.append((cid, key))

    for cid, entries in expired_by_channel.items():
        channel = bot.get_channel(cid)
        if not channel:
            continue
        lines = []
        for key, origin in entries:
            spawned_str = origin.strftime("%I:%M %p") if origin else "Unknown"
            if key in BOSS_NAMES or key.startswith("PCARD") or key.startswith("BCARD"):
                last_spawn_record[(cid, key)] = origin
            lines.append(f"- {key.replace('_', ' ')} (taken at {spawned_str})")
        embed = build_embed(
            "❌ Expired Spawns",
            "The following spawns have now opened (no update received):",
            {"Expired": "\n".join(lines)},
            color=0xE74C3C
        )
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
        if not key.startswith("BCARD"):
            continue
        if (cid, key) in card_auto_extended:
            continue
        # FIX: wider 5-minute window so task jitter doesn't cause misses
        if now >= spawn_time and (now - spawn_time).total_seconds() < 300:
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
    # FIX: no bare except — let unexpected errors surface


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

    time_str, type_key, loc_key = find_time_keyword_location(message.content)

    if not time_str and not type_key and not loc_key:
        await bot.process_commands(message)
        return

    category = None
    key = None
    card_type = None          # FIX: initialise explicitly instead of using locals()
    location_for_display = None

    if type_key and type_key in CARD_KEYS:
        category = "card"
        card_type = type_key
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

    if not category and loc_key:
        loc_norm = loc_key.upper()
        for short, name in ROOM_NAMES.items():
            if loc_norm == short or loc_norm == name.upper():
                category = "room"
                key = short
                break

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
            if T == "BLOOD":
                category = "boss"
                key = "BN"
                break

    if loc_key:
        L = loc_key.upper()
        mapped = LOCATION_ALIASES.get(L, L)
        if mapped in CARD_LOCATIONS:
            location_for_display = CARD_LOCATIONS[mapped]
        elif mapped in ROOM_NAMES:
            location_for_display = ROOM_NAMES[mapped]
        else:
            location_for_display = mapped

    if category == "card" and not location_for_display:
        for tok in re.findall(r"[A-Za-z]+", message.content):
            T = tok.upper()
            mapped = LOCATION_ALIASES.get(T, T)
            if mapped in CARD_LOCATIONS:
                location_for_display = CARD_LOCATIONS[mapped]
                break

    # Determine taken time in PHT
    taken_time_pht = None
    if time_str:
        taken_time_pht = parse_time_string_to_pht(time_str, user_tz)
    if not taken_time_pht:
        taken_time_pht = datetime.now(user_tz).astimezone(PHT)

    # Build spawn_key
    if category == "boss":
        spawn_key = key if key else "UNKNOWN_BOSS"
    elif category == "room":
        spawn_key = key if key else "UNKNOWN_ROOM"
    else:
        # FIX: use the explicitly initialised card_type variable
        card_short = card_type if card_type else "PCARD"
        loc_label = None
        if location_for_display:
            rev_card_locs = {v.upper(): k for k, v in CARD_LOCATIONS.items()}
            loc_label = rev_card_locs.get(location_for_display.upper())
        if loc_key:
            loc_label = LOCATION_ALIASES.get(loc_key.upper(), loc_label)
        loc_label = (loc_label or "UNKNOWN").replace(" ", "")
        spawn_key = f"{card_short}_{loc_label}"

    duration_hours = get_duration_hours(spawn_key)

    now_pht = datetime.now(PHT)
    next_spawn = taken_time_pht + timedelta(hours=duration_hours)

    # FIX: if next_spawn is in the past, it means the report time was stale.
    # Rather than blindly adding a day, warn the user so they can re-enter.
    if next_spawn < now_pht:
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        await message.channel.send(
            f"⚠️ The time you entered puts the next **{spawn_key.replace('_', ' ')}** spawn "
            f"in the past ({next_spawn.strftime('%I:%M %p')} PHT). "
            f"Please re-enter with the correct taken time.",
            delete_after=10
        )
        return

    channel_key = (channel_id, spawn_key)

    # FIX: warn on any existing entry for this spawn_key, not just exact time match
    if channel_key in global_next_spawn:
        existing = global_next_spawn[channel_key]
        if existing == next_spawn:
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            await message.channel.send(
                f"⚠️ The next spawn for **{spawn_key.replace('_', ' ')}** at "
                f"{next_spawn.strftime('%I:%M %p')} PHT is already posted!",
                delete_after=6
            )
            return
        # Different time — this is an update, so allow it through (overwrites old entry)

    if spawn_key in user_sent_times.get(user_id, {}) and user_sent_times[user_id][spawn_key] == next_spawn:
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass
        await message.channel.send(
            f"⚠️ You already sent the same time for **{spawn_key.replace('_', ' ')}**.",
            delete_after=5
        )
        return

    user_sent_times.setdefault(user_id, {})[spawn_key] = next_spawn
    global_next_spawn[channel_key] = next_spawn
    spawn_origin_time[channel_key] = taken_time_pht
    card_auto_extended.discard(channel_key)
    spawn_warned.discard(channel_key)
    last_spawn_record.pop((channel_id, spawn_key), None)

    desc = f"{spawn_key.replace('_', ' ')}"
    fields = {
        "Taken At": taken_time_pht.strftime("%I:%M %p") + " PHT",
        "Next Spawn": f"<t:{unix_ts(next_spawn)}:T> (<t:{unix_ts(next_spawn)}:R>)"
    }
    if location_for_display:
        fields["Location"] = location_for_display

    confirm_embed = build_embed("✅ Timer Set", desc, fields, color=GOLD)
    confirm_msg = await message.channel.send(embed=confirm_embed)
    asyncio.create_task(delete_later(confirm_msg, 300))

    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass

    await update_upcoming_message(message.channel)
    await bot.process_commands(message)


# ---------------- RUN BOT ----------------
bot.run(os.environ["TOKEN"])
