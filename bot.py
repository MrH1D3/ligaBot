import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import random
import asyncio
import csv
import io
from datetime import datetime, timedelta

# ==========================================
# CONFIGURATION
# ==========================================
TOKEN = "MTU2MDk5MTczMjUyOTc2MjMwNA.GWuDhF.3zc1OnC9VO7YTwKp25lLPjh7LWzb9ccJGEvJrs"
DEFAULT_STARTING_BUDGET = 200  # 200M
SEASON_BASE_REWARD = 200       # 200M after each season
PRIZE_1ST = 40                 # 40M
PRIZE_2ND = 20                 # 20M
PRIZE_3RD = 10                 # 10M
LIVE_BID_TIMER = 20            # Seconds per bid in Live Auction
ANTI_SNIPE_MINUTES = 30        # 30-min anti-sniping window for Async Auction

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class AuctionBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        init_db()
        self.async_auction_checker.start()
        await self.tree.sync()
        print("Slash commands synced and database initialized.")

    @tasks.loop(seconds=30)
    async def async_auction_checker(self):
        """Background task to check for expired Asynchronous Auctions."""
        await check_expired_async_auctions(self)

    @async_auction_checker.before_loop
    async def before_checker(self):
        await self.wait_until_ready()

bot = AuctionBot()

# ==========================================
# DATABASE SETUP & HELPERS (SQLite)
# ==========================================
def get_db():
    conn = sqlite3.connect("tournament.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    
    # 1. Users / Managers Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 200
        )
    """)
    
    # 2. Player Pool Table
    # owner_id is NULL if player is free agent
    # custom_price = 0 means use the 18 - 2*n wishlist formula
    c.execute("""
        CREATE TABLE IF NOT EXISTS players (
            player_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            position TEXT NOT NULL,
            custom_price INTEGER DEFAULT 0,
            owner_id INTEGER DEFAULT NULL,
            seasons_left INTEGER DEFAULT 0,
            contract_wage INTEGER DEFAULT 0
        )
    """)
    
    # 3. Wishlists Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS wishlists (
            user_id INTEGER,
            player_id INTEGER,
            PRIMARY KEY (user_id, player_id)
        )
    """)
    
    # 4. Async Auctions Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS async_auctions (
            player_id INTEGER PRIMARY KEY,
            current_bid INTEGER,
            highest_bidder_id INTEGER DEFAULT NULL,
            end_time TEXT,
            channel_id INTEGER,
            active INTEGER DEFAULT 1
        )
    """)
    
    # 5. Season Standings Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS standings (
            user_id INTEGER PRIMARY KEY,
            played INTEGER DEFAULT 0,
            won INTEGER DEFAULT 0,
            drawn INTEGER DEFAULT 0,
            lost INTEGER DEFAULT 0,
            gf INTEGER DEFAULT 0,
            ga INTEGER DEFAULT 0,
            pts INTEGER DEFAULT 0
        )
    """)
    
    # 6. Fixtures Table
    c.execute("""
        CREATE TABLE IF NOT EXISTS fixtures (
            match_id INTEGER PRIMARY KEY AUTOINCREMENT,
            home_id INTEGER,
            away_id INTEGER,
            home_goals INTEGER DEFAULT NULL,
            away_goals INTEGER DEFAULT NULL,
            played INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def calculate_starting_price(player_id: int) -> int:
    """
    Calculates starting price:
    - Formula: clamp(18 - 2*n, 2, 16) where n is wishlist count.
    - If Admin set a custom_price > 0, uses custom_price as base before wishlist discount
      (or exact custom_price if outside standard formula). Here we apply max(2, min(16, 18 - 2*n))
      by default, or custom_price if explicitly set > 0.
    """
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT custom_price FROM players WHERE player_id = ?", (player_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return 2
    
    c.execute("SELECT COUNT(*) as cnt FROM wishlists WHERE player_id = ?", (player_id,))
    n = c.fetchone()["cnt"]
    conn.close()
    
    formula_price = max(2, min(16, 18 - (2 * n)))
    # If admin specified a custom price > 0, use it; otherwise use the wishlist formula
    return row["custom_price"] if row["custom_price"] > 0 else formula_price

def get_available_balance(user_id: int) -> int:
    """Returns user's balance minus any money locked in active async highest bids."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    u = c.fetchone()
    if not u:
        conn.close()
        return 0
    
    c.execute("""
        SELECT COALESCE(SUM(current_bid), 0) as locked 
        FROM async_auctions 
        WHERE highest_bidder_id = ? AND active = 1
    """, (user_id,))
    locked = c.fetchone()["locked"]
    conn.close()
    return u["balance"] - locked

# ==========================================
# MODULE 1: ACCOUNTS & ECONOMY COMMANDS
# ==========================================
@bot.tree.command(name="register", description="Create your tournament account and receive starting budget.")
async def register(interaction: discord.Interaction):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (interaction.user.id,))
    if c.fetchone():
        conn.close()
        return await interaction.response.send_message("❌ You already have an account!", ephemeral=True)
    
    c.execute("INSERT INTO users (user_id, username, balance) VALUES (?, ?, ?)",
              (interaction.user.id, interaction.user.display_name, DEFAULT_STARTING_BUDGET))
    c.execute("INSERT OR IGNORE INTO standings (user_id) VALUES (?)", (interaction.user.id,))
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"✅ Account created for **{interaction.user.display_name}** with **{DEFAULT_STARTING_BUDGET}M** budget!"
    )

@bot.tree.command(name="account", description="View a user's budget and contracted squad.")
@app_commands.describe(member="Leave empty to view your own account")
async def account(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (target.id,))
    user_row = c.fetchone()
    if not user_row:
        conn.close()
        return await interaction.response.send_message(f"❌ **{target.display_name}** is not registered yet.", ephemeral=True)
    
    c.execute("SELECT * FROM players WHERE owner_id = ? ORDER BY position, name", (target.id,))
    players = c.fetchall()
    conn.close()
    
    avail = get_available_balance(target.id)
    locked = user_row["balance"] - avail
    
    embed = discord.Embed(title=f"🏦 Club Profile: {target.display_name}", color=discord.Color.gold())
    embed.add_field(name="Total Balance", value=f"**{user_row['balance']}M**", inline=True)
    embed.add_field(name="Available (Unlocked)", value=f"**{avail}M** *(Locked: {locked}M)*", inline=True)
    embed.add_field(name="Squad Size", value=f"**{len(players)} Players**", inline=True)
    
    if players:
        squad_lines = [
            f"`#{p['player_id']}` **[{p['position']}] {p['name']}** — ⏳ {p['seasons_left']} Season(s) *(Bid: {p['contract_wage']}M/s)*"
            for p in players
        ]
        embed.add_field(name="📋 Contracted Players", value="\n".join(squad_lines[:25]), inline=False)
    else:
        embed.add_field(name="📋 Contracted Players", value="*No players owned yet.*", inline=False)
        
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin_money", description="[ADMIN] Add or remove money from a user's account.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(member="Target user", amount="Amount in Millions (use negative to deduct, e.g., -20)")
async def admin_money(interaction: discord.Interaction, member: discord.Member, amount: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (member.id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return await interaction.response.send_message("❌ User does not have an account.", ephemeral=True)
    
    new_bal = row["balance"] + amount
    c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, member.id))
    conn.commit()
    conn.close()
    sign = "+" if amount >= 0 else ""
    await interaction.response.send_message(f"💰 Adjusted **{member.display_name}**'s balance by **{sign}{amount}M**. New Balance: **{new_bal}M**.")

# ==========================================
# MODULE 2: PLAYER POOL & WISHLIST COMMANDS
# ==========================================
@bot.tree.command(name="player_add", description="[ADMIN] Add an individual player to the auction pool.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(position=[
    app_commands.Choice(name="ATT (Attacker)", value="ATT"),
    app_commands.Choice(name="MID (Midfielder)", value="MID"),
    app_commands.Choice(name="DEF (Defender)", value="DEF"),
    app_commands.Choice(name="GK (Goalkeeper)", value="GK"),
])
@app_commands.describe(custom_price="Leave 0 to use dynamic wishlist formula (18 - 2*n). Set >0 to override.")
async def player_add(interaction: discord.Interaction, name: str, position: app_commands.Choice[str], custom_price: int = 0):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO players (name, position, custom_price) VALUES (?, ?, ?)",
              (name, position.value, custom_price))
    pid = c.lastrowid
    conn.commit()
    conn.close()
    price_str = f"{custom_price}M (Custom Override)" if custom_price > 0 else "16M (Dynamic 18-2n Formula)"
    await interaction.response.send_message(f"✅ Added **[{position.value}] {name}** (ID: `{pid}`) | Starting Price: **{price_str}**")

@bot.tree.command(name="player_batch_csv", description="[ADMIN] Batch add/alter/delete players via CSV file.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(file="CSV columns: action (add/edit/delete), id (for edit/del), name, position (ATT/MID/DEF/GK), custom_price")
async def player_batch_csv(interaction: discord.Interaction, file: discord.Attachment):
    if not file.filename.endswith(".csv"):
        return await interaction.response.send_message("❌ Please upload a valid `.csv` file.", ephemeral=True)
    
    raw = await file.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    
    conn = get_db()
    c = conn.cursor()
    added, edited, deleted = 0, 0, 0
    
    for row in reader:
        action = row.get("action", "add").strip().lower()
        pid = row.get("id", "").strip()
        name = row.get("name", "").strip()
        pos = row.get("position", "MID").strip().upper()
        c_price = int(row.get("custom_price", 0) or 0)
        
        if action == "add" and name:
            c.execute("INSERT INTO players (name, position, custom_price) VALUES (?, ?, ?)", (name, pos, c_price))
            added += 1
        elif action == "edit" and pid:
            c.execute("UPDATE players SET name = ?, position = ?, custom_price = ? WHERE player_id = ?",
                      (name, pos, c_price, int(pid)))
            edited += 1
        elif action == "delete" and pid:
            c.execute("DELETE FROM players WHERE player_id = ?", (int(pid),))
            c.execute("DELETE FROM wishlists WHERE player_id = ?", (int(pid),))
            deleted += 1
            
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"📦 **Batch Update Complete!** Added: `{added}` | Edited: `{edited}` | Deleted: `{deleted}`")

@bot.tree.command(name="player_alter", description="[ADMIN] Alter or delete a single player by ID.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(action=[
    app_commands.Choice(name="Edit Player", value="edit"),
    app_commands.Choice(name="Delete Player", value="delete"),
])
async def player_alter(interaction: discord.Interaction, action: app_commands.Choice[str], player_id: int, new_name: str = None, new_position: str = None, new_custom_price: int = None):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE player_id = ?", (player_id,))
    p = c.fetchone()
    if not p:
        conn.close()
        return await interaction.response.send_message("❌ Player ID not found.", ephemeral=True)
    
    if action.value == "delete":
        c.execute("DELETE FROM players WHERE player_id = ?", (player_id,))
        c.execute("DELETE FROM wishlists WHERE player_id = ?", (player_id,))
        conn.commit()
        conn.close()
        return await interaction.response.send_message(f"🗑️ Deleted player `#{player_id}` (**{p['name']}**).")
    
    name = new_name if new_name else p["name"]
    pos = new_position.upper() if new_position else p["position"]
    c_price = new_custom_price if new_custom_price is not None else p["custom_price"]
    
    c.execute("UPDATE players SET name = ?, position = ?, custom_price = ? WHERE player_id = ?", (name, pos, c_price, player_id))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✏️ Updated `#{player_id}` $\rightarrow$ **[{pos}] {name}** (Custom Price: `{c_price}M`).")

@bot.tree.command(name="wishlist", description="Toggle wishlist on an available player (lowers starting price via 18-2n).")
async def wishlist(interaction: discord.Interaction, player_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE player_id = ?", (player_id,))
    p = c.fetchone()
    if not p:
        conn.close()
        return await interaction.response.send_message("❌ Player not found.", ephemeral=True)
    
    c.execute("SELECT * FROM wishlists WHERE user_id = ? AND player_id = ?", (interaction.user.id, player_id))
    exists = c.fetchone()
    if exists:
        c.execute("DELETE FROM wishlists WHERE user_id = ? AND player_id = ?", (interaction.user.id, player_id))
        msg = f"💔 Removed **{p['name']}** from your wishlist."
    else:
        c.execute("INSERT INTO wishlists (user_id, player_id) VALUES (?, ?)", (interaction.user.id, player_id))
        msg = f"⭐ Added **{p['name']}** to your wishlist!"
    conn.commit()
    conn.close()
    
    new_price = calculate_starting_price(player_id)
    await interaction.response.send_message(f"{msg} Current Starting Price for **{p['name']}** is now **{new_price}M**.", ephemeral=True)

@bot.tree.command(name="pool", description="View available free-agent players in the auction pool.")
async def pool(interaction: discord.Interaction, position_filter: str = None):
    conn = get_db()
    c = conn.cursor()
    if position_filter:
        c.execute("SELECT * FROM players WHERE owner_id IS NULL AND position = ? ORDER BY player_id", (position_filter.upper(),))
    else:
        c.execute("SELECT * FROM players WHERE owner_id IS NULL ORDER BY position, player_id")
    players = c.fetchall()
    
    if not players:
        conn.close()
        return await interaction.response.send_message("📭 No available players found in the pool.", ephemeral=True)
    
    lines = []
    for p in players[:40]:
        c.execute("SELECT COUNT(*) as cnt FROM wishlists WHERE player_id = ?", (p["player_id"],))
        w_cnt = c.fetchone()["cnt"]
        start_p = calculate_starting_price(p["player_id"])
        lines.append(f"`#{p['player_id']}` **[{p['position']}] {p['name']}** — Start: **{start_p}M** *(⭐ {w_cnt} wishlists)*")
    conn.close()
    
    embed = discord.Embed(title="⚽ Available Auction Pool (Free Agents)", description="\n".join(lines), color=discord.Color.blue())
    if len(players) > 40:
        embed.set_footer(text=f"Showing first 40 of {len(players)} available players. Use position_filter (ATT/MID/DEF/GK) to narrow down.")
    await interaction.response.send_message(embed=embed)

# ==========================================
# MODULE 3A: LIVE ROOM AUCTION ENGINE
# ==========================================
class LiveAuctionState:
    def __init__(self, channel: discord.TextChannel):
        self.channel = channel
        self.participants = set()
        self.passed_users = set()
        self.is_running = False
        self.current_player = None
        self.current_bid = 0
        self.highest_bidder = None
        self.timer_remaining = LIVE_BID_TIMER
        self.timer_task = None
        self.message = None

live_rooms = {}  # channel_id -> LiveAuctionState

class ContractSelectView(discord.ui.View):
    def __init__(self, winner_id: int, player_row, winning_bid: int, room: LiveAuctionState):
        super().__init__(timeout=45)
        self.winner_id = winner_id
        self.player = player_row
        self.winning_bid = winning_bid
        self.room = room
        self.completed = False
        
        avail = get_available_balance(winner_id)
        
        # Dynamically create 1, 2, and 3 season buttons and disable unaffordable ones
        for seasons in (1, 2, 3):
            total_cost = winning_bid * seasons
            btn = discord.ui.Button(
                label=f"{seasons} Season{'s' if seasons > 1 else ''} ({total_cost}M)",
                style=discord.ButtonStyle.success if seasons == 1 else discord.ButtonStyle.primary,
                disabled=(total_cost > avail)
            )
            btn.callback = self.make_callback(seasons, total_cost)
            self.add_item(btn)

    def make_callback(self, seasons: int, total_cost: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.winner_id:
                return await interaction.response.send_message("❌ Only the winning bidder can choose the contract duration!", ephemeral=True)
            if self.completed:
                return
            self.completed = True
            self.stop()
            await finalize_contract(interaction.channel, self.winner_id, self.player["player_id"], seasons, self.winning_bid, total_cost)
            await interaction.response.edit_message(content=f"✍️ Contract signed for **{seasons} Season(s)**!", view=None)
            if self.room and self.room.is_running:
                await asyncio.sleep(3)
                await start_next_live_player(self.room)
        return callback

    async def on_timeout(self):
        if not self.completed:
            self.completed = True
            await finalize_contract(self.room.channel, self.winner_id, self.player["player_id"], 1, self.winning_bid, self.winning_bid)
            await self.room.channel.send("⏰ Contract selection timed out! Defaulted to **1 Season**.")
            if self.room and self.room.is_running:
                await asyncio.sleep(3)
                await start_next_live_player(self.room)

async def finalize_contract(channel, winner_id: int, player_id: int, seasons: int, base_bid: int, total_cost: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total_cost, winner_id))
    c.execute("UPDATE players SET owner_id = ?, seasons_left = ?, contract_wage = ? WHERE player_id = ?",
              (winner_id, seasons, base_bid, player_id))
    c.execute("SELECT name, position FROM players WHERE player_id = ?", (player_id,))
    p = c.fetchone()
    conn.commit()
    conn.close()
    await channel.send(
        f"🎉 **SOLD!** <@{winner_id}> signs **[{p['position']}] {p['name']}** for **{seasons} Season(s)**!\n"
        f"💵 Total Paid: **{total_cost}M** (`{base_bid}M` × `{seasons}`)"
    )

class CustomBidModal(discord.ui.Modal, title="Place Custom Bid"):
    bid_amount = discord.ui.TextInput(label="Bid Amount (in Millions, e.g. 25)", placeholder="Enter integer amount...", required=True)

    def __init__(self, room: LiveAuctionState):
        super().__init__()
        self.room = room

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = int(self.bid_amount.value.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Please enter a valid whole number.", ephemeral=True)
        await process_live_bid(interaction, self.room, amt)

class LiveBiddingView(discord.ui.View):
    def __init__(self, room: LiveAuctionState):
        super().__init__(timeout=None)
        self.room = room

    @discord.ui.button(label="+1M Bid", style=discord.ButtonStyle.success, emoji="💵")
    async def bid_plus_one(self, interaction: discord.Interaction, button: discord.ui.Button):
        next_bid = self.room.current_bid if self.room.highest_bidder is None else self.room.current_bid + 1
        await process_live_bid(interaction, self.room, next_bid)

    @discord.ui.button(label="Custom Bid", style=discord.ButtonStyle.primary, emoji="🚀")
    async def bid_custom(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CustomBidModal(self.room))

    @discord.ui.button(label="Pass", style=discord.ButtonStyle.secondary, emoji="🏳️")
    async def pass_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.room.participants:
            return await interaction.response.send_message("❌ You haven't joined this auction room!", ephemeral=True)
        self.room.passed_users.add(interaction.user.id)
        await interaction.response.send_message("🏳️ You passed on this player.", ephemeral=True)
        if len(self.room.passed_users) >= len(self.room.participants) and self.room.highest_bidder is None:
            if self.room.timer_task:
                self.room.timer_task.cancel()
            await resolve_live_player(self.room)

    @discord.ui.button(label="Stop Room (Admin)", style=discord.ButtonStyle.danger)
    async def stop_room(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Only Admins can stop the auction.", ephemeral=True)
        self.room.is_running = False
        if self.room.timer_task:
            self.room.timer_task.cancel()
        await interaction.response.send_message("🛑 **Live Auction Room stopped by Admin.**")

async def process_live_bid(interaction: discord.Interaction, room: LiveAuctionState, bid_amt: int):
    if interaction.user.id not in room.participants:
        return await interaction.response.send_message("❌ Click **Join Room** first (or wait for next lobby)!", ephemeral=True)
    if room.highest_bidder == interaction.user.id:
        return await interaction.response.send_message("⚠️ You are already the highest bidder!", ephemeral=True)
    
    min_required = room.current_bid if room.highest_bidder is None else room.current_bid + 1
    if bid_amt < min_required:
        return await interaction.response.send_message(f"❌ Bid must be at least **{min_required}M**!", ephemeral=True)
    
    avail = get_available_balance(interaction.user.id)
    if bid_amt > avail:
        return await interaction.response.send_message(f"❌ Insufficient budget! Your available balance is **{avail}M**.", ephemeral=True)
    
    room.current_bid = bid_amt
    room.highest_bidder = interaction.user.id
    room.timer_remaining = LIVE_BID_TIMER
    await interaction.response.send_message(f"🔥 **{interaction.user.display_name}** bids **{bid_amt}M** for **{room.current_player['name']}**!")

async def start_next_live_player(room: LiveAuctionState):
    if not room.is_running:
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT * FROM players 
        WHERE owner_id IS NULL 
        AND player_id NOT IN (SELECT player_id FROM async_auctions WHERE active = 1)
    """)
    pool_players = c.fetchall()
    conn.close()
    
    if not pool_players:
        room.is_running = False
        return await room.channel.send("🏁 **All available players in the pool have been auctioned!**")
    
    player = random.choice(pool_players)
    room.current_player = player
    room.current_bid = calculate_starting_price(player["player_id"])
    room.highest_bidder = None
    room.passed_users.clear()
    room.timer_remaining = LIVE_BID_TIMER
    
    embed = build_live_embed(room)
    room.message = await room.channel.send(embed=embed, view=LiveBiddingView(room))
    room.timer_task = asyncio.create_task(run_live_timer(room))

def build_live_embed(room: LiveAuctionState) -> discord.Embed:
    p = room.current_player
    bidder_str = f"<@{room.highest_bidder}>" if room.highest_bidder else "*No bids yet (Starting Price)*"
    embed = discord.Embed(
        title=f"🔨 LIVE AUCTION: [{p['position']}] {p['name']}",
        description=f"⏳ **Time Remaining:** `{room.timer_remaining}s`\n*(Timer resets to {LIVE_BID_TIMER}s on every new bid)*",
        color=discord.Color.green()
    )
    embed.add_field(name="💵 Current Price / Bid", value=f"**{room.current_bid}M**", inline=True)
    embed.add_field(name="👑 Highest Bidder", value=bidder_str, inline=True)
    embed.set_footer(text=f"Player ID: #{p['player_id']} | Passed: {len(room.passed_users)}/{len(room.participants)}")
    return embed

async def run_live_timer(room: LiveAuctionState):
    try:
        while room.timer_remaining > 0 and room.is_running:
            await asyncio.sleep(5)
            room.timer_remaining = max(0, room.timer_remaining - 5)
            if room.message:
                try:
                    await room.message.edit(embed=build_live_embed(room))
                except Exception:
                    pass
        if room.is_running:
            await resolve_live_player(room)
    except asyncio.CancelledError:
        pass

async def resolve_live_player(room: LiveAuctionState):
    if room.message:
        try:
            await room.message.edit(view=None)
        except Exception:
            pass
            
    p = room.current_player
    if room.highest_bidder is None:
        await room.channel.send(f"💨 **{p['name']}** went **UNSOLD**! Moving to next random player in 3 seconds...")
        await asyncio.sleep(3)
        await start_next_live_player(room)
    else:
        view = ContractSelectView(room.highest_bidder, p, room.current_bid, room)
        await room.channel.send(
            f"🔔 **GOING ONCE, GOING TWICE, SOLD!**\n"
            f"<@{room.highest_bidder}> won the bid for **[{p['position']}] {p['name']}** at **{room.current_bid}M/season**!\n"
            f"👉 **Select how many seasons (1–3) you want to sign them for:**",
            view=view
        )

class LobbyView(discord.ui.View):
    def __init__(self, room: LiveAuctionState):
        super().__init__(timeout=None)
        self.room = room

    @discord.ui.button(label="Join Auction Room", style=discord.ButtonStyle.primary, emoji="✋")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id = ?", (interaction.user.id,))
        if not c.fetchone():
            conn.close()
            return await interaction.response.send_message("❌ Run `/register` first to create an account!", ephemeral=True)
        conn.close()
        
        self.room.participants.add(interaction.user.id)
        names = [f"<@{uid}>" for uid in self.room.participants]
        await interaction.response.send_message(f"✅ Joined! Current Room Participants ({len(names)}): {', '.join(names)}")

    @discord.ui.button(label="Start Random Picks (Admin)", style=discord.ButtonStyle.success, emoji="▶️")
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Only an Admin can start the auction.", ephemeral=True)
        if not self.room.participants:
            return await interaction.response.send_message("❌ At least 1 user must join the room first!", ephemeral=True)
        if self.room.is_running:
            return await interaction.response.send_message("⚠️ Auction is already running!", ephemeral=True)
        
        self.room.is_running = True
        await interaction.response.send_message("🚀 **Live Auction Starting Now! Drawing first random player...**")
        await start_next_live_player(self.room)

@bot.tree.command(name="auction_live_lobby", description="[ADMIN] Open a Live Auction Room where random players are drawn one by one.")
@app_commands.checks.has_permissions(administrator=True)
async def auction_live_lobby(interaction: discord.Interaction):
    room = LiveAuctionState(interaction.channel)
    live_rooms[interaction.channel.id] = room
    embed = discord.Embed(
        title="🏟️ Live Auction Room Open!",
        description=(
            "Click **Join Auction Room** below to enter.\n"
            "• System randomly picks available players.\n"
            "• Bid `+1M` or a custom amount.\n"
            "• Winner chooses **1, 2, or 3 seasons** (`Cost = HighestBid × Seasons`)."
        ),
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed, view=LobbyView(room))

# ==========================================
# MODULE 3B: ASYNCHRONOUS AUCTION ENGINE (WITH 30-MIN ANTI-SNIPE)
# ==========================================
@bot.tree.command(name="auction_async_start", description="[ADMIN] Lay out all available pool players for asynchronous bidding.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(duration_hours="How many hours the auction runs (e.g., 24). Includes 30-min anti-snipe.")
async def auction_async_start(interaction: discord.Interaction, duration_hours: float):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE owner_id IS NULL")
    free_players = c.fetchall()
    if not free_players:
        conn.close()
        return await interaction.response.send_message("❌ No free agents available in the pool.", ephemeral=True)
    
    end_dt = datetime.utcnow() + timedelta(hours=duration_hours)
    end_iso = end_dt.isoformat()
    
    count = 0
    for p in free_players:
        start_price = calculate_starting_price(p["player_id"])
        c.execute("""
            INSERT OR REPLACE INTO async_auctions (player_id, current_bid, highest_bidder_id, end_time, channel_id, active)
            VALUES (?, ?, NULL, ?, ?, 1)
        """, (p["player_id"], start_price, end_iso, interaction.channel.id))
        count += 1
        
    conn.commit()
    conn.close()
    
    unix_ts = int(end_dt.replace(tzinfo= discord.utils.utcnow().tzinfo).timestamp())
    await interaction.response.send_message(
        f"📢 **Asynchronous Auction Started for `{count}` Players!**\n"
        f"⏰ Base End Time: <t:{unix_ts}:F> (<t:{unix_ts}:R>)\n"
        f"🛡️ **Anti-Sniping Rule Active:** Any bid placed when `< {ANTI_SNIPE_MINUTES} mins` remain resets that player's clock to `{ANTI_SNIPE_MINUTES} mins`!\n"
        f"👉 Use `/async_market` to view all active players and `/async_bid` to place bids."
    )

@bot.tree.command(name="async_market", description="View all active Asynchronous Auction listings and timers.")
async def async_market(interaction: discord.Interaction, position_filter: str = None):
    conn = get_db()
    c = conn.cursor()
    query = """
        SELECT a.*, p.name, p.position 
        FROM async_auctions a
        JOIN players p ON a.player_id = p.player_id
        WHERE a.active = 1
    """
    params = ()
    if position_filter:
        query += " AND p.position = ?"
        params = (position_filter.upper(),)
    query += " ORDER BY a.player_id"
    
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return await interaction.response.send_message("📭 No active asynchronous auctions right now.", ephemeral=True)
    
    lines = []
    for r in rows[:35]:
        end_dt = datetime.fromisoformat(r["end_time"])
        unix_ts = int(end_dt.replace(tzinfo=discord.utils.utcnow().tzinfo).timestamp())
        bidder = f"<@{r['highest_bidder_id']}>" if r["highest_bidder_id"] else "*None (Start)*"
        lines.append(
            f"`#{r['player_id']}` **[{r['position']}] {r['name']}** — **{r['current_bid']}M** by {bidder} | Ends <t:{unix_ts}:R>"
        )
        
    embed = discord.Embed(title="📊 Live Asynchronous Auction Market", description="\n".join(lines), color=discord.Color.teal())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="async_bid", description="Place a bid on a player in the Asynchronous Auction.")
@app_commands.describe(player_id="Player ID from /async_market", bid_amount="Leave 0 to increment by +1M, or enter custom amount")
async def async_bid(interaction: discord.Interaction, player_id: int, bid_amount: int = 0):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT a.*, p.name, p.position 
        FROM async_auctions a 
        JOIN players p ON a.player_id = p.player_id 
        WHERE a.player_id = ? AND a.active = 1
    """, (player_id,))
    auc = c.fetchone()
    if not auc:
        conn.close()
        return await interaction.response.send_message("❌ That player is not in an active async auction.", ephemeral=True)
    
    now = datetime.utcnow()
    end_dt = datetime.fromisoformat(auc["end_time"])
    if now >= end_dt:
        conn.close()
        return await interaction.response.send_message("⏰ Bidding time for this player has already expired!", ephemeral=True)
    
    if auc["highest_bidder_id"] == interaction.user.id:
        conn.close()
        return await interaction.response.send_message("⚠️ You are already the highest bidder on this player!", ephemeral=True)
    
    min_req = auc["current_bid"] if auc["highest_bidder_id"] is None else auc["current_bid"] + 1
    final_bid = min_req if bid_amount == 0 else bid_amount
    
    if final_bid < min_req:
        conn.close()
        return await interaction.response.send_message(f"❌ Minimum valid bid for **{auc['name']}** is **{min_req}M**.", ephemeral=True)
    
    avail = get_available_balance(interaction.user.id)
    if final_bid > avail:
        conn.close()
        return await interaction.response.send_message(
            f"❌ Insufficient unlocked budget! Your available balance (excluding active bids) is **{avail}M**.", ephemeral=True
        )
    
    # 30-Minute Anti-Sniping Check
    remaining = end_dt - now
    snipe_triggered = False
    if remaining < timedelta(minutes=ANTI_SNIPE_MINUTES):
        end_dt = now + timedelta(minutes=ANTI_SNIPE_MINUTES)
        snipe_triggered = True
        
    c.execute("""
        UPDATE async_auctions 
        SET current_bid = ?, highest_bidder_id = ?, end_time = ? 
        WHERE player_id = ?
    """, (final_bid, interaction.user.id, end_dt.isoformat(), player_id))
    conn.commit()
    conn.close()
    
    unix_ts = int(end_dt.replace(tzinfo=discord.utils.utcnow().tzinfo).timestamp())
    snipe_msg = f"\n🛡️ **Anti-Snipe Triggered!** Timer extended to <t:{unix_ts}:R>!" if snipe_triggered else f" (Ends <t:{unix_ts}:R>)"
    await interaction.response.send_message(
        f"🔨 **{interaction.user.display_name}** placed a **{final_bid}M** bid on `#{player_id}` **[{auc['position']}] {auc['name']}**!{snipe_msg}"
    )

class AsyncContractExtendView(discord.ui.View):
    """Allows an async auction winner to upgrade their automatic 1-season contract to 2 or 3 seasons."""
    def __init__(self, winner_id: int, player_id: int, player_name: str, base_bid: int):
        super().__init__(timeout=86400)  # 24h window to upgrade
        self.winner_id = winner_id
        self.player_id = player_id
        self.player_name = player_name
        self.base_bid = base_bid
        self.upgraded = False

    @discord.ui.button(label="Upgrade to 2 Seasons (+1x Bid)", style=discord.ButtonStyle.primary)
    async def extend_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_upgrade(interaction, 2)

    @discord.ui.button(label="Upgrade to 3 Seasons (+2x Bid)", style=discord.ButtonStyle.success)
    async def extend_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_upgrade(interaction, 3)

    async def handle_upgrade(self, interaction: discord.Interaction, target_seasons: int):
        if interaction.user.id != self.winner_id:
            return await interaction.response.send_message("❌ Only the winning manager can extend this contract.", ephemeral=True)
        if self.upgraded:
            return await interaction.response.send_message("⚠️ Contract already upgraded!", ephemeral=True)
        
        extra_seasons = target_seasons - 1
        extra_cost = self.base_bid * extra_seasons
        avail = get_available_balance(self.winner_id)
        if extra_cost > avail:
            return await interaction.response.send_message(f"❌ You need **{extra_cost}M** available to upgrade to {target_seasons} seasons!", ephemeral=True)
        
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (extra_cost, self.winner_id))
        c.execute("UPDATE players SET seasons_left = ? WHERE player_id = ?", (target_seasons, self.player_id))
        conn.commit()
        conn.close()
        
        self.upgraded = True
        self.stop()
        await interaction.response.edit_message(
            content=f"✍️ <@{self.winner_id}> extended **{self.player_name}**'s contract to **{target_seasons} Seasons** (Total Cost: **{self.base_bid * target_seasons}M**)!",
            view=None
        )

async def check_expired_async_auctions(bot_instance: commands.Bot):
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT a.*, p.name, p.position 
        FROM async_auctions a
        JOIN players p ON a.player_id = p.player_id
        WHERE a.active = 1
    """)
    rows = c.fetchall()
    now = datetime.utcnow()
    
    for r in rows:
        end_dt = datetime.fromisoformat(r["end_time"])
        if now >= end_dt:
            c.execute("UPDATE async_auctions SET active = 0 WHERE player_id = ?", (r["player_id"],))
            channel = bot_instance.get_channel(r["channel_id"])
            
            if r["highest_bidder_id"] is None:
                conn.commit()
                if channel:
                    await channel.send(f"⏰ Async Auction ended for **[{r['position']}] {r['name']}** — **UNSOLD**.")
            else:
                winner_id = r["highest_bidder_id"]
                bid = r["current_bid"]
                # Award 1 season immediately so offline users don't stall, and offer button to extend to 2 or 3 seasons
                c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (bid, winner_id))
                c.execute("UPDATE players SET owner_id = ?, seasons_left = 1, contract_wage = ? WHERE player_id = ?",
                          (winner_id, bid, r["player_id"]))
                conn.commit()
                if channel:
                    view = AsyncContractExtendView(winner_id, r["player_id"], r["name"], bid)
                    await channel.send(
                        f"🏁 **ASYNC AUCTION WON!** <@{winner_id}> signs **[{r['position']}] {r['name']}** for **{bid}M** (1 Season settled)!\n"
                        f"👇 *Click below if you want to extend their contract to 2 or 3 seasons:*",
                        view=view
                    )
    conn.close()

# ==========================================
# MODULE 4: TOURNAMENT, STANDINGS & AUTOMATIONS
# ==========================================
@bot.tree.command(name="season_start", description="[ADMIN] Generate Round-Robin fixtures for all registered users to start the season.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(double_round_robin="True = Home & Away (2x matches), False = Single match per opponent")
async def season_start(interaction: discord.Interaction, double_round_robin: bool = False):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = [r["user_id"] for r in c.fetchall()]
    if len(users) < 2:
        conn.close()
        return await interaction.response.send_message("❌ Need at least 2 registered users to start a season.", ephemeral=True)
    
    # Reset standings and fixtures
    c.execute("DELETE FROM fixtures")
    c.execute("DELETE FROM standings")
    for uid in users:
        c.execute("INSERT INTO standings (user_id) VALUES (?)", (uid,))
        
    matchups = []
    for i in range(len(users)):
        for j in range(i + 1, len(users)):
            matchups.append((users[i], users[j]))
            if double_round_robin:
                matchups.append((users[j], users[i]))
                
    random.shuffle(matchups)
    for home, away in matchups:
        c.execute("INSERT INTO fixtures (home_id, away_id) VALUES (?, ?)", (home, away))
        
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"🏆 **New Season Initialized!** Generated **{len(matchups)} matches** across **{len(users)} managers**.\n"
        f"Use `/fixtures` to view unplayed matches and `/match_report` to upload results."
    )

@bot.tree.command(name="fixtures", description="View remaining unplayed tournament matches.")
async def fixtures(interaction: discord.Interaction):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM fixtures WHERE played = 0 ORDER BY match_id")
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return await interaction.response.send_message("✅ All scheduled matches have been played!", ephemeral=True)
    
    lines = [f"`Match #{r['match_id']}`: <@{r['home_id']}> **vs** <@{r['away_id']}>" for r in rows[:30]]
    embed = discord.Embed(title=f"📅 Unplayed Fixtures ({len(rows)} Remaining)", description="\n".join(lines), color=discord.Color.orange())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="match_report", description="[ADMIN] Upload a match result. Automatically ends season when last match is played.")
@app_commands.checks.has_permissions(administrator=True)
async def match_report(interaction: discord.Interaction, match_id: int, home_goals: int, away_goals: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM fixtures WHERE match_id = ?", (match_id,))
    m = c.fetchone()
    if not m:
        conn.close()
        return await interaction.response.send_message("❌ Match ID not found.", ephemeral=True)
    if m["played"] == 1:
        conn.close()
        return await interaction.response.send_message("⚠️ This match has already been recorded!", ephemeral=True)
    
    h_id, a_id = m["home_id"], m["away_id"]
    c.execute("UPDATE fixtures SET home_goals = ?, away_goals = ?, played = 1 WHERE match_id = ?",
              (home_goals, away_goals, match_id))
    
    # Calculate Points & W/D/L
    if home_goals > away_goals:
        h_pts, a_pts, h_w, h_d, h_l, a_w, a_d, a_l = 3, 0, 1, 0, 0, 0, 0, 1
    elif home_goals < away_goals:
        h_pts, a_pts, h_w, h_d, h_l, a_w, a_d, a_l = 0, 3, 0, 0, 1, 1, 0, 0
    else:
        h_pts, a_pts, h_w, h_d, h_l, a_w, a_d, a_l = 1, 1, 0, 1, 0, 0, 1, 0
        
    c.execute("""
        UPDATE standings 
        SET played = played + 1, won = won + ?, drawn = drawn + ?, lost = lost + ?,
            gf = gf + ?, ga = ga + ?, pts = pts + ?
        WHERE user_id = ?
    """, (h_w, h_d, h_l, home_goals, away_goals, h_pts, h_id))
    
    c.execute("""
        UPDATE standings 
        SET played = played + 1, won = won + ?, drawn = drawn + ?, lost = lost + ?,
            gf = gf + ?, ga = ga + ?, pts = pts + ?
        WHERE user_id = ?
    """, (a_w, a_d, a_l, away_goals, home_goals, a_pts, a_id))
    
    c.execute("SELECT COUNT(*) as rem FROM fixtures WHERE played = 0")
    remaining = c.fetchone()["rem"]
    conn.commit()
    conn.close()
    
    await interaction.response.send_message(
        f"✅ **Match #{match_id} Recorded:** <@{h_id}> **{home_goals} - {away_goals}** <@{a_id}>"
    )
    
    if remaining == 0:
        await trigger_end_of_season(interaction.channel)

@bot.tree.command(name="standings", description="View the current league table.")
async def standings(interaction: discord.Interaction):
    embed = build_standings_embed()
    await interaction.response.send_message(embed=embed)

def build_standings_embed() -> discord.Embed:
    conn = get_db()
    c = conn.cursor()
    # Sorted strictly by: 1. Points (pts), 2. Goal Difference (gf - ga), 3. Goals For (gf)
    c.execute("""
        SELECT s.*, u.username, (s.gf - s.ga) as gd
        FROM standings s
        JOIN users u ON s.user_id = u.user_id
        ORDER BY s.pts DESC, gd DESC, s.gf DESC
    """)
    rows = c.fetchall()
    conn.close()
    
    header = f"`{'#':<2} {'Team':<14} {'P':>2} {'W':>2} {'D':>2} {'L':>2} {'GF':>3} {'GA':>3} {'GD':>4} {'PTS':>3}`"
    lines = [header, "─" * 44]
    for idx, r in enumerate(rows, start=1):
        name = (r["username"][:12] + "..") if len(r["username"]) > 14 else r["username"]
        gd_str = f"+{r['gd']}" if r["gd"] > 0 else str(r["gd"])
        lines.append(
            f"`{idx:<2} {name:<14} {r['played']:>2} {r['won']:>2} {r['drawn']:>2} {r['lost']:>2} {r['gf']:>3} {r['ga']:>3} {gd_str:>4} {r['pts']:>3}`"
        )
        
    return discord.Embed(title="🏆 League Standings", description="\n".join(lines), color=discord.Color.gold())

async def trigger_end_of_season(channel: discord.TextChannel):
    """
    Automations executed when all matches are played:
    1. Final Standings sorted by PTS -> GD -> GF
    2. Base Grant (+200M) + Prize Money (1st: 40M, 2nd: 20M, 3rd: 10M) added to remaining budget
    3. Decrement player contracts by 1 season; expired contracts (0 left) return to the free agent pool
    """
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT s.*, u.username, (s.gf - s.ga) as gd
        FROM standings s
        JOIN users u ON s.user_id = u.user_id
        ORDER BY s.pts DESC, gd DESC, s.gf DESC
    """)
    ranked = c.fetchall()
    
    payout_lines = []
    for idx, r in enumerate(ranked, start=1):
        prize = PRIZE_1ST if idx == 1 else (PRIZE_2ND if idx == 2 else (PRIZE_3RD if idx == 3 else 0))
        total_grant = SEASON_BASE_REWARD + prize
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total_grant, r["user_id"]))
        c.execute("SELECT balance FROM users WHERE user_id = ?", (r["user_id"],))
        new_bal = c.fetchone()["balance"]
        
        medal = "🥇" if idx == 1 else ("🥈" if idx == 2 else ("🥉" if idx == 3 else f"`#{idx}`"))
        payout_lines.append(
            f"{medal} <@{r['user_id']}>: `+{SEASON_BASE_REWARD}M Base` + `+{prize}M Prize` $\rightarrow$ **New Budget: {new_bal}M**"
        )
        
    # Contract Decay Automation
    c.execute("UPDATE players SET seasons_left = seasons_left - 1 WHERE owner_id IS NOT NULL")
    c.execute("SELECT name FROM players WHERE owner_id IS NOT NULL AND seasons_left <= 0")
    expired_players = [row["name"] for row in c.fetchall()]
    
    c.execute("""
        UPDATE players 
        SET owner_id = NULL, seasons_left = 0, contract_wage = 0 
        WHERE owner_id IS NOT NULL AND seasons_left <= 0
    """)
    conn.commit()
    conn.close()
    
    standings_embed = build_standings_embed()
    standings_embed.title = "🏁 FINAL SEASON STANDINGS"
    
    summary_embed = discord.Embed(
        title="🎉 End of Season Automations & Payouts",
        description="\n".join(payout_lines),
        color=discord.Color.green()
    )
    expired_str = ", ".join(expired_players) if expired_players else "*None (all multi-season contracts still active)*"
    summary_embed.add_field(
        name="🔄 Contract Expirations (Returned to Auction Pool)",
        value=expired_str[:1024],
        inline=False
    )
    
    await channel.send(content="🚨 **ALL SEASON MATCHES COMPLETED!** 🚨", embeds=[standings_embed, summary_embed])

bot.run(TOKEN)