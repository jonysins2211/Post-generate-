# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
# 🔹 Core & Third-Party Library Imports
# ---------------------------------------------------------------------------
import os
import io
import re
import logging
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
import requests
from typing import List, Dict

# --- Environment Variable Loading ---
from dotenv import load_dotenv

# --- Media & Image Processing ---
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import cv2
from thefuzz import fuzz
# --- Telegram & Database ---
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import UserNotParticipant, ChatAdminRequired
from motor.motor_asyncio import AsyncIOMotorClient

# --- Web Server (for Keep-Alive) ---
from flask import Flask

# ---------------------------------------------------------------------------
# 🔹 Configuration and Initial Setup
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# --- Bot & API Configuration ---
API_ID = int(os.environ.get("API_ID", "29961422"))
API_HASH = os.environ.get("API_HASH", "cba915c79809dc0806676db7052b2a83")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb+srv://movieloverz11220:KtsMU9bBA9E3aIHh@cluster0.uqb5pin.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "1ba98a04426a253bc7cb4be687abe2ed")

# --- Channel & Owner Information ---
AUTH_CHANNEL = int(os.environ.get("AUTH_CHANNEL", "-1002230197603"))
OWNER_ID = int(os.environ.get("OWNER_ID", "949657126"))

# ---------------------------------------------------------------------------
# 🔹 Global Variables & Client Initialization
# ---------------------------------------------------------------------------
# --- Database Setup ---
mongo_client = AsyncIOMotorClient(MONGO_URL)
db = mongo_client["UltimatePostBotDB"]
users_collection = db["users"]
reactions_collection = db["reactions"]
logger.info("✅ MongoDB database successfully connected.")

# --- Pyrogram Client ---
app = Client("UltimatePostBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- For storing user conversations and data ---
user_conversations = {}

# --- Thread Pool for blocking tasks ---
executor = ThreadPoolExecutor(max_workers=10)

# ---------------------------------------------------------------------------
# 🔹 Flask Web Server (for Keep-Alive)
# ---------------------------------------------------------------------------
flask_app = Flask(__name__)
@flask_app.route("/")
def index():
    return "Bot is running perfectly!", 200

def run_flask():
    try:
        flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    except Exception as e:
        logger.error(f"Flask app crashed: {e}")

threading.Thread(target=run_flask, daemon=True).start()
logger.info("🚀 Flask web server started.")

# ---------------------------------------------------------------------------
# 🔹 Helper Functions
# ---------------------------------------------------------------------------

async def loading_animation(message: Message, stop_event: asyncio.Event):
    """
    Edits a message repeatedly to show a large clock animation until the stop_event is set.
    """
    
    animation_frames = ["🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚", "🕛"]
    idx = 0
    text = "⏳ Please wait, checking your channels..."
    
    while not stop_event.is_set():
        try:
            
            display_text = f"{animation_frames[idx]}\n\n{text}"
            
            await message.edit_text(display_text)
            idx = (idx + 1) % len(animation_frames)
            await asyncio.sleep(0.3)  
            
        except Exception:
            
            break



async def is_subscribed(bot: Client, user_id: int):
    try:
        await bot.get_chat_member(AUTH_CHANNEL, user_id)
        return True
    except UserNotParticipant:
        return False
    except Exception as e:
        logger.error(f"Subscription check failed for user {user_id}: {e}")
        return False

async def ensure_bot_admin_rights(bot: Client, channel_id: int) -> bool:
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(channel_id, me.id)
        if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            privileges = member.privileges
            if privileges and privileges.can_post_messages:
                return True
            else:
                logger.warning(f"Bot is admin in {channel_id} but lacks 'can_post_messages' permission.")
                return False
        logger.warning(f"Bot is not an admin in {channel_id}. Status: {member.status}")
        return False
    except ChatAdminRequired:
        logger.error(f"Cannot check admin rights for {channel_id}: Bot needs to be an admin to get chat member list.")
        return False
    except Exception as e:
        logger.error(f"Failed to check admin rights for channel {channel_id}: {e}")
        return False

async def save_channel(user_id: int, channel_id: int, channel_title: str):
    if not await ensure_bot_admin_rights(app, channel_id):
        raise ValueError("Bot must be an admin in the channel with 'Post Messages' permission.")

    user = await users_collection.find_one({"user_id": user_id})
    if user and any(ch["id"] == channel_id for ch in user.get("channels", [])):
        return False, "This channel is already in your list."

    await users_collection.update_one(
        {"user_id": user_id},
        {"$push": {"channels": {"id": channel_id, "title": channel_title}}},
        upsert=True
    )
    return True, f"Channel '{channel_title}' added successfully!"

async def shorten_link(user_id: int, long_url: str):
    user_data = await users_collection.find_one({'user_id': user_id})
    if not user_data or 'shortener_api' not in user_data or 'shortener_url' not in user_data:
        return long_url

    api_key = user_data['shortener_api']
    base_url = user_data['shortener_url']
    api_url = f"https://{base_url}/api?api={api_key}&url={long_url}"
    
    try:
        response = await asyncio.get_event_loop().run_in_executor(
            executor, lambda: requests.get(api_url, timeout=10)
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "success" and data.get("shortenedUrl"):
            return data["shortenedUrl"]
    except Exception as e:
        logger.error(f"Shortener API Error for user {user_id}: {e}")
    return long_url

def format_runtime(minutes: int):
    if not isinstance(minutes, int) or minutes <= 0: return "N/A"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours > 0 else f"{mins}m"

# ---------------------------------------------------------------------------
# 🔹 TMDB API & Smart Poster Generation
# ---------------------------------------------------------------------------
def search_tmdb(query: str):
    """
    Searches TMDB. If no direct results, returns a list of fuzzy suggestions.
    Returns a tuple: (status, results)
    status can be 'DIRECT', 'SUGGESTIONS', or 'NO_RESULTS'.
    """
    logger.info(f"Performing TMDB search for query: '{query}'")
    
    year, name = None, query.strip()
    match = re.search(r'(.+?)\s*\(?(\d{4})\)?$', query)
    if match: name, year = match.group(1).strip(), match.group(2)

    # --- Step 1: Direct Search ---
    search_url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={name}" + (f"&year={year}" if year else "")
    try:
        r = requests.get(search_url, timeout=10)
        r.raise_for_status()
        results = [res for res in r.json().get("results", []) if res.get("media_type") in ["movie", "tv"]]
        
        if results:
            logger.info(f"Direct search successful. Found {len(results)} results.")
            return 'DIRECT', results[:5]

    except requests.exceptions.RequestException as e:
        logger.error(f"TMDB direct search request failed: {e}")
        return 'NO_RESULTS', []

    # --- Step 2: Fuzzy Search for Suggestions (if direct search fails) ---
    logger.info("Direct search failed. Trying to find suggestions...")
    try:
        fuzzy_search_url = f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={name}"
        r_fuzzy = requests.get(fuzzy_search_url, timeout=10)
        r_fuzzy.raise_for_status()
        candidates = [res for res in r_fuzzy.json().get("results", []) if res.get("media_type") in ["movie", "tv"]]

        if not candidates:
            return 'NO_RESULTS', []

        scored_candidates = []
        for candidate in candidates:
            title = candidate.get('title') or candidate.get('name', '')
            score = fuzz.ratio(name.lower(), title.lower())
            if score > 65:  # মিল ৬৫% এর বেশি হলে তাকে সাজেশন হিসেবে গণ্য করা হবে
                candidate['fuzzy_score'] = score
                scored_candidates.append(candidate)
        
        if scored_candidates:
            # সেরা মিলগুলো উপরে দেখানোর জন্য স্কোর অনুযায়ী সাজানো হলো
            scored_candidates.sort(key=lambda x: x['fuzzy_score'], reverse=True)
            logger.info(f"Found {len(scored_candidates)} suggestions.")
            return 'SUGGESTIONS', scored_candidates[:5]

    except requests.exceptions.RequestException as e:
        logger.error(f"TMDB fuzzy search request failed: {e}")

    return 'NO_RESULTS', []

        
def get_tmdb_details(media_type: str, media_id: int):
    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}"
    try:
        r = requests.get(url, timeout=10); r.raise_for_status(); return r.json()
    except Exception as e:
        logger.error(f"TMDB Details Error: {e}"); return None

def download_file(url: str, filename: str):
    if not os.path.exists(filename):
        logger.info(f"Downloading {filename}...")
        try:
            r = requests.get(url, timeout=20, stream=True)
            r.raise_for_status()
            with open(filename, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f"Downloaded {filename} successfully.")
            return True
        except Exception as e:
            logger.error(f"Could not download {filename}. Error: {e}")
            return False
    return True

def _watermark_poster_sync(poster_url: str, watermark_text: str, badge_text: str = None):
    font_files = {
        "bold": ("Poppins-Bold.ttf", "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf"),
        "badge": ("HindSiliguri-Bold.ttf", "https://github.com/google/fonts/raw/main/ofl/hindsiliguri/HindSiliguri-Bold.ttf")
    }
    cascade_file = ("haarcascade_frontalface_default.xml", "https://raw.githubusercontent.com/opencv/opencv/master/data/haarcascades/haarcascade_frontalface_default.xml")
    
    # --- Download necessary files ---
    for key, (name, url) in font_files.items():
        download_file(url, name)
    cascade_path = cascade_file[0] if download_file(cascade_file[1], cascade_file[0]) else None

    bold_font_path = font_files["bold"][0]
    badge_font_path = font_files["badge"][0]

    if not poster_url: return None, "Poster URL not found."
    try:
        img_data = requests.get(poster_url, timeout=20).content
        original_img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        img = Image.new("RGBA", original_img.size)
        img.paste(original_img)
        draw = ImageDraw.Draw(img)

        # --- Badge Processing ---
        if badge_text:
            try:
                badge_font_size = int(img.width / 9)
                badge_font = ImageFont.truetype(badge_font_path, badge_font_size)
                
                bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
                text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
                x, y = (img.width - text_width) / 2, img.height * 0.03
                
                # Face detection to avoid covering faces
                if cascade_path and cv2 is not None:
                    cv_image = np.array(original_img.convert('RGB'))
                    gray = cv2.cvtColor(cv_image, cv2.COLOR_RGB2GRAY)
                    face_cascade = cv2.CascadeClassifier(cascade_path)
                    faces = face_cascade.detectMultiScale(gray, 1.1, 5)
                    if any(y < (fy + fh) and (y + text_height) > fy for (fx, fy, fw, fh) in faces):
                        y = img.height * 0.25
                
                padding = int(badge_font_size * 0.1)
                rect_layer = Image.new('RGBA', img.size, (0,0,0,0))
                ImageDraw.Draw(rect_layer).rectangle(
                    (x - padding, y - padding, x + text_width + padding, y + text_height + padding), 
                    fill=(0, 0, 0, 140)
                )
                img = Image.alpha_composite(img, rect_layer)
                draw = ImageDraw.Draw(img) # Re-initialize draw object after composite
                draw.text((x, y), badge_text, font=badge_font, fill=(255, 255, 0))

            except IOError:
                logger.error(f"Badge font '{badge_font_path}' not found. Badge will not be added.")
            except Exception as e:
                logger.error(f"Error while adding badge: {e}")

        # --- Watermark Processing ---
        if watermark_text:
            try:
                font_size = int(img.width / 12)
                font = ImageFont.truetype(bold_font_path, font_size)
                bbox = draw.textbbox((0, 0), watermark_text, font=font)
                text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
                wx, wy = (img.width - text_width) / 2, img.height - text_height - (img.height * 0.05)
                draw.text((wx + 2, wy + 2), watermark_text, font=font, fill=(0, 0, 0, 128))
                draw.text((wx, wy), watermark_text, font=font, fill=(255, 255, 255, 230))
            except IOError:
                logger.error(f"Watermark font '{bold_font_path}' not found. Watermark will not be added.")
            except Exception as e:
                logger.error(f"Error while adding watermark: {e}")

        buffer = io.BytesIO()
        buffer.name = "poster.png"
        img.convert("RGB").save(buffer, "PNG")
        buffer.seek(0)
        return buffer, None
    except Exception as e:
        logger.error(f"Image processing error: {e}")
        return None, f"Image processing failed: {e}"

async def watermark_poster(poster_url: str, watermark_text: str, badge_text: str = None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        executor, _watermark_poster_sync, poster_url, watermark_text, badge_text
    )

# ---------------------------------------------------------------------------
# 🔹 Start & General Command Handlers
# ---------------------------------------------------------------------------
@app.on_message(filters.private & filters.command("start"))
async def start_handler(bot, msg: Message):
    if not await is_subscribed(bot, msg.from_user.id):
        try:
            chat = await bot.get_chat(AUTH_CHANNEL)
            invite_link = chat.invite_link or await bot.export_chat_invite_link(AUTH_CHANNEL)
            btns = [
                [InlineKeyboardButton(f"✇ Join {chat.title} ✇", url=invite_link)],
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_check")]
            ]
            return await msg.reply_photo(
                photo="https://i.postimg.cc/xdkd1h4m/IMG-20250715-153124-952.jpg",
                caption=f"👋 Hello {msg.from_user.mention},\n\nPlease join our channel to use this bot.",
                reply_markup=InlineKeyboardMarkup(btns)
            )

        except Exception as e:
            logger.error(f"Could not get invite link for AUTH_CHANNEL {AUTH_CHANNEL}: {e}")
            error_buttons = [
                [InlineKeyboardButton("✪ ꜱᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ ✪", url="https://t.me/Movie_loverzz")]
            ]
            return await msg.reply_text(
                "⚠️ **Oops! Something went wrong while creating the Auth Check.**\n\n"
                "Please wait a moment while we look into the issue. 🕒\n"
                "You can also report this problem directly to our support team.\n\n"
                "🔹 Once reported, our team will fix it as soon as possible.\n\n"
                "Thank you for your patience 💖",
                reply_markup=InlineKeyboardMarkup(error_buttons)
            )

    buttons = [
        [InlineKeyboardButton(" 🎬 ʜᴏᴡ ᴛᴏ ᴄʀᴇᴀᴛᴇ ᴀ ᴘᴏꜱᴛ", callback_data="create_post_help")],
        [
            InlineKeyboardButton("✪ ꜱᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ", url="https://t.me/Movie_loverzz"),
            InlineKeyboardButton("〄 ᴜᴘᴅᴀᴛᴇs ᴄʜᴀɴɴᴇʟ", url="https://t.me/MovieEntertainment4u")
        ],
        [
            InlineKeyboardButton("⚙️ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="settings_menu"),
            InlineKeyboardButton("〆 ᴀʙᴏᴜᴛ 〆", callback_data="about_bot")
        ],
        [InlineKeyboardButton("✧ ᴄʀᴇᴀᴛᴏʀ ✧", url="https://t.me/Mladminbot")]
    ]

    await msg.reply_photo(
        photo="https://envs.sh/vxr.jpg",
        caption=(
            f"👋 ᴡᴇʟᴄᴏᴍᴇ, {msg.from_user.mention}!\n\n"
            "🎬 ɪ’ᴍ ʏᴏᴜʀ **ᴀᴅᴠᴀɴᴄᴇᴅ ᴘᴏꜱᴛ ɢᴇɴᴇʀᴀᴛᴏʀ ʙᴏᴛ** — ʙᴜɪʟᴛ ᴛᴏ ᴄʀᴇᴀᴛᴇ ʙᴇᴀᴜᴛɪꜰᴜʟ ᴍᴏᴠɪᴇ & ꜱᴇʀɪᴇꜱ ᴘᴏꜱᴛꜱ ᴇꜰꜰᴏʀᴛʟᴇꜱꜱʟʏ!\n\n"
            "✨ **ʜᴇʀᴇ’ꜱ ᴡʜᴀᴛ ɪ ᴄᴀɴ ᴅᴏ ꜰᴏʀ ʏᴏᴜ:**\n"
            "1️⃣ **ꜱᴍᴀʀᴛ ᴀᴜᴛᴏ ᴘᴏꜱᴛ:**\n"
            "   ᴊᴜꜱᴛ ꜱᴇɴᴅ ᴍᴇ ᴀ ᴍᴏᴠɪᴇ ᴏʀ ᴛᴠ ꜱᴇʀɪᴇꜱ ɴᴀᴍᴇ — ɪ’ʟʟ ꜰᴇᴛᴄʜ ᴀʟʟ ᴛʜᴇ ᴅᴇᴛᴀɪʟꜱ ᴀɴᴅ ɢᴇɴᴇʀᴀᴛᴇ ᴀ ꜱᴛᴜɴɴɪɴɢ ᴘᴏꜱᴛ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ!\n\n"
            "2️⃣ **Qᴜɪᴄᴋ ᴍᴀɴᴜᴀʟ ᴘᴏꜱᴛ:**\n"
            "   ꜱᴇɴᴅ ᴍᴇ ᴀɴʏ ᴘʜᴏᴛᴏ ᴏʀ ᴠɪᴅᴇᴏ, ᴀɴᴅ ɪ’ʟʟ ʟᴇᴛ ʏᴏᴜ ᴄʜᴏᴏꜱᴇ ᴀ ᴄʜᴀɴɴᴇʟ ꜰʀᴏᴍ ʏᴏᴜʀ ʟɪꜱᴛ.\n"
            "   ɪ’ʟʟ ɪɴꜱᴛᴀɴᴛʟʏ ᴘᴏꜱᴛ ɪᴛ ᴡɪᴛʜ ʏᴏᴜʀ ᴄᴜꜱᴛᴏᴍ ʜᴇᴀᴅᴇʀ, ꜰᴏᴏᴛᴇʀ, ᴄᴀᴘᴛɪᴏɴ, ʙᴜᴛᴛᴏɴꜱ, ᴀɴᴅ ʀᴇᴀᴄᴛɪᴏɴꜱ!\n\n"
            "💡 **ᴛʀʏ ɪᴛ ɴᴏᴡ:**\n"
            "ꜱᴇɴᴅ ᴀ ᴍᴏᴠɪᴇ ɴᴀᴍᴇ ᴏʀ ᴀ ᴘʜᴏᴛᴏ/ᴠɪᴅᴇᴏ ᴛᴏ ɢᴇᴛ ꜱᴛᴀʀᴛᴇᴅ!\n\n"
            "📢 Your Creative Posting Assistant 💫"
        ),
        reply_markup=InlineKeyboardMarkup(buttons)
            )

# ---------------------------------------------------------------------------
# 🔹 Post Creation Flow (Triggered by any text that is not a command)
# ---------------------------------------------------------------------------
ALL_COMMANDS = [
    "start", "addchannel", "mychannels", "delchannel", 
    "setcap", "delcap", "seecap", 
    "setheader", "delheader", "seeheader", "setfooter", "delfooter", "seefooter", # Add these
    "addbutton", "mybuttons", "delbutton", "clearbuttons", 
    "setwatermark", "delwatermark", 
    "setapi", "delapi", 
    "setdomain", "deldomain", 
    "settutorial", "deltutorial", 
    "badge", "delbadge",
    "settings", "stats", "broadcast", "help"
]

@app.on_message(filters.private & filters.text & ~filters.command(ALL_COMMANDS) & ~filters.forwarded)
async def post_creation_entry(bot, msg: Message):
    uid = msg.from_user.id
    query = msg.text.strip()

    bot_status_prefixes = ("🔍", "✅", "❌", "⚠️", "👍", "⏳", "🖼️", "📝")
    if query.startswith(bot_status_prefixes) or len(query) > 150:
        logger.warning(f"Ignoring likely bot status message or long query from user {uid}: {query[:50]}...")
        return

    convo = user_conversations.get(uid)

    # --- New, more robust gatekeeper logic ---
    if convo:
        # These are the states where the bot is actively waiting for a specific text input.
        active_input_states = [
            "wait_lang", "wait_480p", "wait_720p", "wait_1080p", 
            "wait_season_number", "ask_episode_or_done", 
            "wait_season_link", "wait_episode_link"
        ]
        
        # If the user is in one of the active input states, let the conversation_handler manage the text.
        if convo.get("state") in active_input_states:
            return await conversation_handler(bot, msg)
        
        # For any other situation where a `convo` exists (e.g., a preview is shown, or it's awaiting a forward),
        # it's considered an unfinished task. Block the new request.
        else:
            await msg.reply_text(
                "⚠️ **You have an unfinished task!**\n\n"
                "You have a pending post that has not been completed. Please post it to a channel or cancel it using the button below before starting a new one.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Cancel Previous Task", callback_data="cancel_process")]]
                )
            )
            return
    # --- End of new gatekeeper logic ---
    
    # If no conversation exists, proceed with the new search as normal.
    processing_msg = await msg.reply_text(f"🔍 Searching for `{query}`...")
    
    loop = asyncio.get_running_loop()
    status, results = await loop.run_in_executor(executor, search_tmdb, query)
    
    if status == 'NO_RESULTS':
        return await processing_msg.edit_text("❌ No results found. Please check the spelling and try again.")
    
    buttons = []
    for r in results:
        media_icon = '🎬' if r['media_type'] == 'movie' else '📺'
        title = r.get('title') or r.get('name')
        year = (r.get('release_date') or r.get('first_air_date') or '----').split('-')[0]
        buttons.append([InlineKeyboardButton(f"{media_icon} {title} ({year})", callback_data=f"select_post_{r['media_type']}_{r['id']}")])
    
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_process")])
    
    if status == 'DIRECT':
        await processing_msg.edit_text("**👇 Choose from the results:**", reply_markup=InlineKeyboardMarkup(buttons))
    elif status == 'SUGGESTIONS':
        await processing_msg.edit_text(
            "❌ **No exact match found. Did you mean one of these?**",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

@app.on_callback_query(filters.regex("^select_post_"))
async def select_post_callback(bot, cq: CallbackQuery):
    try:
        parts = cq.data.split("_")
        if len(parts) < 4: raise ValueError("Invalid callback data")
        media_type, mid = parts[2], int(parts[3])
    except (ValueError, IndexError):
        return await cq.message.edit_text("❌ Invalid callback data.")

    await cq.answer("⏳ Fetching details...", show_alert=False)
    
    loop = asyncio.get_running_loop()
    details = await loop.run_in_executor(executor, get_tmdb_details, media_type, mid)
    
    if not details:
        return await cq.message.edit_text("❌ Failed to get details from TMDB.")

    uid = cq.from_user.id
    user_conversations[uid] = {"details": details, "links": {}, "seasons": {}, "state": "wait_lang"}
    
    await cq.message.edit_text(
        f"✅ Selected: **{details.get('title') or details.get('name')}**\n\n"
        f"💬 Enter the language for the post (e.g., Bengali, Hindi), or type **skip** to use the default (`{details.get('original_language')}`)."
    )

# ========================= 🧩 BLOCK 1: conversation_handler() =========================

async def conversation_handler(bot, msg: Message):
    uid = msg.from_user.id
    text = msg.text.strip()
    convo = user_conversations.get(uid)
    if not convo or "state" not in convo:
        return

    state = convo["state"]
    media_type = "movie" if "release_date" in convo["details"] else "tv"

    async def finish_and_generate_post(final_msg_text: str):
        """Helper to finalize link collection and start post generation."""
        if not convo.get('seasons'):
            if uid in user_conversations: del user_conversations[uid]
            return await msg.reply_text("❌ No links were added. Process cancelled.")
        
        convo['links'] = convo.get('seasons', {})
        convo["state"] = "generating_post"
        status_msg = await msg.reply_text(final_msg_text)
        await generate_final_post_preview(bot, uid, msg.chat.id, status_msg)

    async def process_link(quality: str, next_state: str, next_prompt: str):
        """Handles movie quality link inputs with shortener support"""
        if text.lower() != 'skip':
            shortened = await shorten_link(uid, text)
            convo["links"][quality] = shortened
            await msg.reply_text(f"✅ {quality} link added.")
        else:
            await msg.reply_text(f"☑️ {quality} link skipped.")
        convo["state"] = next_state
        await msg.reply_text(next_prompt)

    # ========== Language Setup ==========
    if state == "wait_lang":
        convo["language"] = text.capitalize() if text.lower() != 'skip' else convo["details"].get('original_language', 'en').capitalize()

        if media_type == "movie":
            convo["state"] = "wait_480p"
            await msg.reply_text("✅ Language set. Now send the **480p** link or type `skip`.")
        else:
            convo["state"] = "wait_season_number"
            await msg.reply_text("✅ Language set. Now enter the **Season number** (e.g., 1, 2).")

    # ========== Movie Section ==========
    elif state == "wait_480p":
        await process_link("480p", "wait_720p", "Now send the **720p** link or type `skip`.")

    elif state == "wait_720p":
        await process_link("720p", "wait_1080p", "Now send the **1080p** link or type `skip`.")

    elif state == "wait_1080p":
        if text.lower() != 'skip':
            convo["links"]["1080p"] = await shorten_link(uid, text)
        convo["state"] = "generating_post"
        status_msg = await msg.reply_text("✅ All info collected. Generating post...")
        await generate_final_post_preview(bot, uid, msg.chat.id, status_msg)

    # ========== TV Series Section ==========
    elif state == "wait_season_number":
        if text.lower() == 'skip':
            return await finish_and_generate_post("✅ Link collection skipped. Generating post...")
        
        if text.lower() == 'done':
            return await finish_and_generate_post("✅ All season info collected. Generating post...")

        if not text.isdigit() or int(text) <= 0:
            return await msg.reply_text("❌ Invalid number. Please enter a correct season number.")

        convo['current_season'] = text
        convo['state'] = 'ask_episode_or_done'
        await msg.reply_text(
            f"✅ Season {text} selected.\n\n"
            "**What's next?**\n"
            "🔹 To add episodes, enter the **Episode number** (e.g., `5`).\n"
            "🔹 To add a link for the whole season, type `done`.\n"
            "🔹 To finish adding links now, type `skip`."
        )

    elif state == "ask_episode_or_done":
        if text.lower() == 'skip':
            return await finish_and_generate_post("✅ Link collection skipped. Generating post...")

        if text.lower() == 'done':
            convo['state'] = 'wait_season_link'
            await msg.reply_text(f"👉 Send the **download link** for Season {convo['current_season']}.")
        elif text.isdigit():
            convo['current_episode'] = text
            convo['state'] = 'wait_episode_link'
            await msg.reply_text(f"🎬 Now send the **link for Season {convo['current_season']} Episode {text}**.")
        else:
            await msg.reply_text("⚠️ Invalid input. Enter an episode number (e.g., `5`), `done`, or `skip`.")

    elif state == "wait_season_link":
        season_num = convo.get('current_season')
        shortened = await shorten_link(uid, text)
        convo.setdefault('seasons', {})[season_num] = shortened
        convo['state'] = 'wait_season_number'
        await msg.reply_text(
            f"✅ Link for Season {season_num} added.\n\n"
            "👉 Enter next season number, type `done` to finish, or `skip` to generate the post now."
        )

    elif state == "wait_episode_link":
        season_num = convo.get('current_season')
        episode_num = convo.get('current_episode')
        shortened = await shorten_link(uid, text)
        key = f"{season_num}x{episode_num}"  # Season 1 Episode 5 → 1x5
        convo.setdefault('seasons', {})[key] = shortened
        convo['state'] = 'ask_episode_or_done'
        await msg.reply_text(
            f"✅ Link for **Season {season_num} Episode {episode_num}** added.\n\n"
            "👉 Enter the next episode number, type `done` to move to the next season, or `skip` to generate the post now."
    )

# ---------------------------------------------------------------------------
# 🔹 Final Post Preview & Posting
# ---------------------------------------------------------------------------
async def generate_final_post_preview(bot, uid, chat_id, status_msg: Message):
    convo = user_conversations.get(uid)
    if not convo: return await status_msg.edit_text("❌ Session expired.")

    user_data = await users_collection.find_one({'user_id': uid}) or {}
    
    await status_msg.edit_text("🖼️ Generating smart poster...")
    poster_url = f"https://image.tmdb.org/t/p/w500{convo['details']['poster_path']}" if convo['details'].get('poster_path') else None
    
    badge_text = user_conversations.get(uid, {}).pop('temp_badge_text', None)
    watermark = user_data.get('watermark_text')
    
    poster, error = await watermark_poster(poster_url, watermark, badge_text=badge_text)
    
    if error: await bot.send_message(chat_id, f"⚠️ **Poster generation error:** `{error}`")

    await status_msg.edit_text("📝 Generating caption and buttons...")
    caption = await generate_channel_caption(convo, user_data)
    
    if len(caption) > 1024:
        error_msg = (
            "❌ **Caption Too Long!**\n\n"
            f"Your generated caption is **{len(caption)}** characters long, but Telegram only allows **1024** for photos.\n\n"
            "Please shorten your `/setheader`, `/setfooter`, or `/setcap` and try again."
        )
        await status_msg.edit_text(error_msg)
        if uid in user_conversations:
            del user_conversations[uid]
        return

    inline_keyboard = [
        [InlineKeyboardButton("👍 0", callback_data="react_DUMMY_like"), InlineKeyboardButton("❤️ 0", callback_data="react_DUMMY_love")]
    ]
    for btn in user_data.get("custom_buttons", []):
        inline_keyboard.append([InlineKeyboardButton(btn["text"], url=btn["url"])])

    await status_msg.delete()
    
    convo['final_post'] = {
        'caption': caption,
        'poster': poster.getvalue() if poster else None,
        'buttons': inline_keyboard
    }
    
    preview_msg = await bot.send_photo(
        chat_id=chat_id,
        photo=poster if poster else "https://via.placeholder.com/500x750.png?text=No+Poster",
        caption=caption,
        reply_markup=InlineKeyboardMarkup(inline_keyboard)
    )
    
    
    channel_status_msg = await preview_msg.reply_text("⏳ Please wait...")
    stop_event = asyncio.Event()
    animation_task = asyncio.create_task(loading_animation(channel_status_msg, stop_event))

    try:
        saved_channels = []
        if user_data.get('channels'):
            for ch in user_data.get('channels', []):
                if await ensure_bot_admin_rights(bot, ch['id']):
                    saved_channels.append(ch)
        
        
        stop_event.set()
        await animation_task

        if saved_channels:
            channel_buttons = [[InlineKeyboardButton(f"📢 Post to {ch['title']}", callback_data=f"postto_{ch['id']}")] for ch in saved_channels]
            channel_buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_process")])
            await channel_status_msg.edit_text(
                "**👆 This is a preview. Choose a channel to post:**",
                reply_markup=InlineKeyboardMarkup(channel_buttons)
            )
        else:
            convo['state'] = 'awaiting_forward_for_post'
            await channel_status_msg.edit_text(
                "✅ **Preview generated!**\n\n"
                "You have no channels saved where I have admin rights.\n\n"
                "**To post this, please:**\n"
                "1. Make me an admin in your desired channel.\n"
                "2. Forward any message from that channel to me.\n\n"
                "I will automatically post this content there."
            )
    except Exception as e:
        logger.error(f"Error during channel check in preview: {e}")
        stop_event.set()
        await animation_task
        await channel_status_msg.edit_text("❌ An unexpected error occurred while checking channels.")

# ========================= 🧩 BLOCK 2: generate_channel_caption() =========================
async def generate_channel_caption(convo: dict, user_data: dict):
    data = convo["details"]
    links = convo["links"]
    is_tv = "first_air_date" in data

    custom_header = user_data.get('custom_header')
    custom_footer = user_data.get('custom_footer')

    info = {
        "title": data.get("title") or data.get("name") or "N/A",
        "year": (data.get("release_date") or data.get("first_air_date") or "----")[:4],
        "genres": ", ".join([g["name"] for g in data.get("genres", [])[:3]]) or "N/A",
        "rating": f"{data.get('vote_average', 0):.1f}",
        "language": convo.get('language', 'N/A'),
        "runtime": format_runtime(data.get("runtime") if not is_tv else (data.get("episode_run_time") or [0])[0]),
    }

    caption_header = (
        f"<blockquote>🎬 **{info['title']} ({info['year']})** ══╗\n"
        f"⭐ **IMDb:** `{info['rating']}/10`\n"
        f"🎭 **Genre:** `{info['genres']}`\n"
        f"🈳 **Language:** `{info['language']}`\n"
        f"⏰ **Runtime:** `{info['runtime']}`\n</blockquote>"
        #f"╚══════════════════════╝"
    )

    download_section_header = "🔰 **Download Links** 🔰"
    download_links = ""
    
    LINK_LENGTH_THRESHOLD = 32

    # ========== TV Series Section (With New Short Link Format) ==========
    if is_tv:
        tv_links = []
        sorted_keys = sorted(links.keys(), key=lambda k: tuple(map(int, k.split('x'))) if 'x' in k else (int(k), -1))
        
        for key in sorted_keys:
            link = links.get(key)
            if not link:
                continue

            # --- Logic for Long Links (Unchanged) ---
            if len(link) > LINK_LENGTH_THRESHOLD:
                is_episode = 'x' in key
                if is_episode:
                    emoji = "🎬"
                    s, e = key.split('x')
                    hyperlink_label = f"Download S{s.zfill(2)} E{e.zfill(2)}"
                else:
                    emoji = "📁"
                    hyperlink_label = f"Download Season {key}"
                formatted_line = f"{emoji} [{hyperlink_label}]({link})"
            
            # --- New Logic for Short Links (As per your request) ---
            else:
                is_episode = 'x' in key
                if is_episode:
                    s, e = key.split('x')
                    # Using "Session" as you requested in the example
                    label = f"Session {s} Episode {e}"
                else:
                    label = f"Session {key}"
                
                # Creates the two-line, clickable format
                formatted_line = f"📁 {label}\n🔗 {link}"
            
            tv_links.append(formatted_line)
        
        # Use a double newline to separate each two-line block
        download_links = "\n\n".join(tv_links)

    # ========== Movie Section (With New Short Link Format) ==========
    else:
        movie_links = []
        for quality in ["480p", "720p", "1080p"]:
            link = links.get(quality)
            if not link:
                continue
            
            # --- Logic for Long Links (Unchanged) ---
            if len(link) > LINK_LENGTH_THRESHOLD:
                emoji = "🎞️" if quality == "480p" else "📺" if quality == "720p" else "🎥"
                formatted_line = f"{emoji} [Download {quality}]({link})"
            
            # --- New Logic for Short Links (As per your request) ---
            else:
                # Creates the two-line, clickable format
                label = quality.upper()
                formatted_line = f"📁 {label}\n🔗{link}"
            
            movie_links.append(formatted_line)
        
        # Use a double newline to create space between each entry
        download_links = "\n\n".join(movie_links)

    # --- Tutorial section and final merge (Unchanged) ---
    tutorial_section = ""
    if user_data.get('tutorial_link'):
        tutorial_url = user_data['tutorial_link']
        tutorial_section = (
            "╭━❰📚 ʜᴏᴡ ᴛᴏ ᴏᴘᴇɴ ʟɪɴᴋꜱ ᴛᴜᴛᴏʀɪᴀʟ ❱━⊱\n"
            f"┃    <a href='{tutorial_url}'>📥 𝗪𝗔𝗧𝗖𝗛 𝗧𝗨𝗧ᴏʀɪᴀʟ ɴᴏᴡ ▶️</a>\n"
            "╰━━━━━━━━━━━━━━━━⊱"
        )
    
    final_parts = []
    if custom_header:
        final_parts.append(custom_header)
    
    final_parts.append(caption_header)

    if download_links:
        final_parts.append(download_section_header + "\n" + download_links)
    if tutorial_section:
        final_parts.append(tutorial_section)
    
    if custom_footer:
        final_parts.append(custom_footer)

    return "\n\n".join(final_parts)


async def post_to_channel(bot: Client, user_id: int, channel_id: int, status_message: Message):
    convo = user_conversations.get(user_id)
    if not convo or 'final_post' not in convo:
        await status_message.edit_text("❌ Session expired! Please start over.")
        return

    await status_message.edit_text("⏳ Posting to channel...")
    final_post = convo['final_post']
    try:
        posted_msg = await bot.send_photo(
            chat_id=channel_id,
            photo=io.BytesIO(final_post['poster']) if final_post['poster'] else "https://via.placeholder.com/500x750.png?text=No+Poster",
            caption=final_post['caption']
        )
        await reactions_collection.insert_one({"message_id": posted_msg.id, "chat_id": channel_id, "reactions": {"like": [], "love": []}})
        
        final_buttons = final_post['buttons']
        final_buttons[0][0].callback_data = f"react_{posted_msg.id}_like"
        final_buttons[0][1].callback_data = f"react_{posted_msg.id}_love"
        
        await posted_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(final_buttons))
        
        chat = await bot.get_chat(channel_id)
        await status_message.edit_text(f"✅ **Successfully posted to '{chat.title}'!**")
    except Exception as e:
        logger.error(f"Failed to post to channel {channel_id} for user {user_id}. Error: {e}")
        await status_message.edit_text(f"❌ **Failed to post to channel.**\n\n**Error:** `{e}`")
    finally:
        if user_id in user_conversations:
            del user_conversations[user_id]

@app.on_callback_query(filters.regex("^postto_"))
async def post_to_channel_callback(bot, cq: CallbackQuery):
    await cq.answer()
    user_id = cq.from_user.id
    channel_id = int(cq.data.split("_")[1])
    await post_to_channel(bot, user_id, channel_id, cq.message)


# ---------------------------------------------------------------------------
# 🔹 Direct Media Posting & Forward Handler
# ---------------------------------------------------------------------------
@app.on_message(filters.private & (filters.photo | filters.video) & ~filters.forwarded)
async def direct_media_handler(bot, msg: Message):
    if not await is_subscribed(bot, msg.from_user.id):
        try:
            chat = await bot.get_chat(AUTH_CHANNEL)
            invite_link = chat.invite_link or await bot.export_chat_invite_link(AUTH_CHANNEL)
            btns = [
                [InlineKeyboardButton(f"✇ Join {chat.title} ✇", url=invite_link)],
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_check")]
            ]
            return await msg.reply_photo(
                photo="https://i.postimg.cc/xdkd1h4m/IMG-20250715-153124-952.jpg",
                caption=f"👋 Hello {msg.from_user.mention},\n\nPlease join our channel to use this bot.",
                reply_markup=InlineKeyboardMarkup(btns)
            )

        except Exception as e:
            logger.error(f"Could not get invite link for AUTH_CHANNEL {AUTH_CHANNEL}: {e}")
            error_buttons = [
                [InlineKeyboardButton("✪ ꜱᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ ✪", url="https://t.me/MOVIE_LOVERZZ")]
            ]
            return await msg.reply_text(
                "⚠️ **Oops! Something went wrong while creating the Auth Check.**\n\n"
                "Please wait a moment while we look into the issue. 🕒\n"
                "You can also report this problem directly to our support team.\n\n"
                "🔹 Once reported, our team will fix it as soon as possible.\n\n"
                "Thank you for your patience 💖",
                reply_markup=InlineKeyboardMarkup(error_buttons)
            )
    uid = msg.from_user.id
    convo = user_conversations.get(uid)

    # --- New, more robust gatekeeper logic ---
    # If a user has ANY active conversation or unfinished task, block the new media submission.
    if convo:
        await msg.reply_text(
            "⚠️ **You have an unfinished task!**\n\n"
            "You have a pending post that has not been completed. Please post it to a channel or cancel it using the button below before starting a new one.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel Previous Task", callback_data="cancel_process")]]
            )
        )
        return
    # --- End of new gatekeeper logic ---

    # If no conversation exists, proceed with handling the direct media.
    status_msg = await msg.reply_text("⏳ Please wait, checking your channels...")
    stop_event = asyncio.Event()
    animation_task = asyncio.create_task(loading_animation(status_msg, stop_event))

    try:
        user = await users_collection.find_one({"user_id": uid})
        
        buttons = []
        if user and user.get("channels"):
            for ch in user["channels"]:
                if await ensure_bot_admin_rights(bot, ch['id']):
                    buttons.append([InlineKeyboardButton(ch["title"], callback_data=f"sendto_{msg.id}_{ch['id']}")])

        stop_event.set()
        await animation_task
        
        if not buttons:
            user_conversations[uid] = {
                "state": "awaiting_forward_for_direct_media",
                "media_message_id": msg.id
            }
            await status_msg.edit_text(
                "✅ **Media received!**\n\n"
                "You have no channels saved where I have admin rights.\n\n"
                "**To post this, please:**\n"
                "1. Make me an admin in your desired channel.\n"
                "2. Forward any message from that channel to me.\n\n"
                "I will automatically post this media there."
            )
        else:
            await status_msg.edit_text(
                "📤 **Choose a channel to post this media to:**",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
    except Exception as e:
        logger.error(f"Error in direct media handler: {e}")
        stop_event.set()
        await animation_task
        await status_msg.edit_text("❌ An unexpected error occurred.")
        
async def post_direct_media_to_channel(bot: Client, user_id: int, channel_id: int, media_msg_id: int, status_message: Message):
    """Helper function to post a direct media message to a channel."""
    if not await ensure_bot_admin_rights(bot, channel_id):
        await status_message.edit_text("❌ Bot is not an admin or lacks 'Post Messages' permission!")
        return

    try:
        await status_message.edit_text("⏳ Preparing to post...")
        media_msg = await bot.get_messages(user_id, media_msg_id)
        user_data = await users_collection.find_one({"user_id": user_id}) or {}
        
        # হেডার, ফুটার এবং ক্যাপশন একত্রিত করা হচ্ছে
        caption_parts = []
        if user_data.get("custom_header"): caption_parts.append(user_data["custom_header"])
        if media_msg.caption: caption_parts.append(media_msg.caption.html)
        if user_data.get("custom_caption"): caption_parts.append(user_data["custom_caption"])
        
        if user_data.get('tutorial_link'):
            tutorial_url = user_data['tutorial_link']
            tutorial_text = (
                "╭━❰📚 ʜᴏᴡ ᴛᴏ ᴏᴘᴇɴ ʟɪɴᴋꜱ ᴛᴜᴛᴏʀɪᴀʟ ❱━⊱\n"
                f"┃    <a href='{tutorial_url}'>📥 𝗪𝗔𝗧𝗖𝗛 𝗧𝗨𝗧𝗢𝗥𝗜𝗔𝗟 𝗡𝗢𝗪 ▶️</a>\n"
                "╰━━━━━━━━━━━━━━━━⊱"
            )
            caption_parts.append(tutorial_text)

        if user_data.get("custom_footer"): caption_parts.append(user_data["custom_footer"])
        final_caption = "\n\n".join(caption_parts)

        if len(final_caption) > 1024:
            await status_message.edit_text("❌ **Caption Too Long!** Limit is 1024 characters.")
            return

        
        all_buttons = [
            [InlineKeyboardButton("👍 0", callback_data="react_DUMMY_like"), InlineKeyboardButton("❤️ 0", callback_data="react_DUMMY_love")]
        ]
        for btn in user_data.get("custom_buttons", []):
            all_buttons.append([InlineKeyboardButton(btn["text"], url=btn["url"])])
        
        
        copied_msg = await media_msg.copy(chat_id=channel_id, caption=final_caption)
        await reactions_collection.insert_one({"message_id": copied_msg.id, "chat_id": channel_id, "reactions": {"like": [], "love": []}})
        
        all_buttons[0][0].callback_data = f"react_{copied_msg.id}_like"
        all_buttons[0][1].callback_data = f"react_{copied_msg.id}_love"

        await copied_msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(all_buttons))
        
        chat = await bot.get_chat(channel_id)
        await status_message.edit_text(f"✅ **Successfully posted to '{chat.title}'!**")

    except Exception as e:
        logger.error(f"Failed to post direct media: {e}")
        await status_message.edit_text(f"❌ Failed to post. Error: {e}")

@app.on_callback_query(filters.regex("^sendto_"))
async def direct_media_post_callback(bot, cq: CallbackQuery):
    
    await cq.answer("✅ Posting...", show_alert=False)
    
    try:
        
        _, msg_id, channel_id = cq.data.split("_")
        msg_id, channel_id = int(msg_id), int(channel_id)
        user_id = cq.from_user.id

        
        await post_direct_media_to_channel(bot, user_id, channel_id, msg_id, cq.message)

    except Exception as e:
        
        logger.error(f"Error in direct media callback: {e}")
        await cq.message.edit_text(f"❌ An unexpected error occurred: {e}")
        
@app.on_message(filters.private & filters.forwarded)
async def forward_handler(bot, msg: Message):
    if not await is_subscribed(bot, msg.from_user.id):
        try:
            chat = await bot.get_chat(AUTH_CHANNEL)
            invite_link = chat.invite_link or await bot.export_chat_invite_link(AUTH_CHANNEL)
            btns = [
                [InlineKeyboardButton(f"✇ Join {chat.title} ✇", url=invite_link)],
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_check")]
            ]
            return await msg.reply_photo(
                photo="https://i.postimg.cc/xdkd1h4m/IMG-20250715-153124-952.jpg",
                caption=f"👋 Hello {msg.from_user.mention},\n\nPlease join our channel to use this bot.",
                reply_markup=InlineKeyboardMarkup(btns)
            )

        except Exception as e:
            logger.error(f"Could not get invite link for AUTH_CHANNEL {AUTH_CHANNEL}: {e}")
            error_buttons = [
                [InlineKeyboardButton("✪ ꜱᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ ✪", url="https://t.me/MovieEntertainment4u")]
            ]
            return await msg.reply_text(
                "⚠️ **Oops! Something went wrong while creating the Auth Check.**\n\n"
                "Please wait a moment while we look into the issue. 🕒\n"
                "You can also report this problem directly to our support team.\n\n"
                "🔹 Once reported, our team will fix it as soon as possible.\n\n"
                "Thank you for your patience 💖",
                reply_markup=InlineKeyboardMarkup(error_buttons)
            )
    if not msg.forward_from_chat or msg.forward_from_chat.type != enums.ChatType.CHANNEL:
        return
    
    uid = msg.from_user.id
    channel = msg.forward_from_chat
    
    convo = user_conversations.get(uid)
    
    status_msg = await msg.reply_text(f"⏳ Processing channel **{channel.title}**...")

    try:
        
        saved, status_text = await save_channel(uid, channel.id, channel.title)

        
        if convo and convo.get('state') == 'awaiting_forward_for_post':
            
            await post_to_channel(bot, uid, channel.id, status_msg)
        
        elif convo and convo.get('state') == 'awaiting_forward_for_direct_media':
            
            media_msg_id = convo.get("media_message_id")
            if media_msg_id:
                await post_direct_media_to_channel(bot, uid, channel.id, media_msg_id, status_msg)
            else:
                await status_msg.edit_text("❌ Error: Could not find the original media to post.")
        
        else:
            
            await status_msg.edit_text(f"✅ {status_text}" if saved else f"⚠️ {status_text}")
            
    except ValueError as e: 
        await status_msg.edit_text(f"❌ **Could not add channel '{channel.title}'.**\n\n**Reason:** `{e}`\n\nPlease ensure I am an administrator in the channel and have the 'Post Messages' permission, then forward a message again.")
    except Exception as e:
        logger.error(f"Error saving forwarded channel {channel.id} for user {uid}: {e}")
        await status_msg.edit_text(f"❌ An unexpected error occurred while processing the channel. Error: {e}")
    finally:
        
        if convo and convo.get('state') == 'awaiting_forward_for_direct_media':
             if uid in user_conversations:
                del user_conversations[uid]


# --- (Rest of the handlers: reaction, settings, commands, etc. remain largely the same) ---

# ---------------------------------------------------------------------------
# 🔹 Reaction Handler
# ---------------------------------------------------------------------------
@app.on_callback_query(filters.regex("^react_"))
async def reaction_handler(bot, cq: CallbackQuery):
    try:
        data_parts = cq.data.split("_", 2)
        if len(data_parts) < 3 or "DUMMY" in data_parts[1]:
            return await cq.answer("This is a preview button.", show_alert=True)

        _, msg_id, reaction = data_parts
        msg_id, user_id = int(msg_id), cq.from_user.id

        post = await reactions_collection.find_one({"message_id": msg_id})
        if not post:
            return await cq.answer("Sorry, reaction data for this post not found.", show_alert=True)
        
        # Toggle logic
        other_reaction = "love" if reaction == "like" else "like"
        current_reaction_users = post["reactions"].get(reaction, [])
        other_reaction_users = post["reactions"].get(other_reaction, [])

        if user_id in current_reaction_users:
            current_reaction_users.remove(user_id) # Un-react
        else:
            if user_id in other_reaction_users:
                other_reaction_users.remove(user_id) # Switch reaction
            current_reaction_users.append(user_id) # Add new reaction
        
        await reactions_collection.update_one(
            {"message_id": msg_id}, 
            {"$set": {f"reactions.{reaction}": current_reaction_users, f"reactions.{other_reaction}": other_reaction_users}}
        )

        like_count = len(current_reaction_users) if reaction == "like" else len(other_reaction_users)
        love_count = len(current_reaction_users) if reaction == "love" else len(other_reaction_users)

        current_keyboard = cq.message.reply_markup.inline_keyboard
        current_keyboard[0] = [
            InlineKeyboardButton(f"👍 {like_count}", callback_data=f"react_{msg_id}_like"),
            InlineKeyboardButton(f"❤️ {love_count}", callback_data=f"react_{msg_id}_love")
        ]
        
        await cq.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(current_keyboard))
        await cq.answer("✅ Your reaction has been updated!", show_alert=False)

    except Exception as e:
        logger.error(f"Reaction handling error: {e}")
        await cq.answer("❌ Error updating reaction.", show_alert=True)

# ---------------------------------------------------------------------------
# 🔹 Settings, Menus & Other Commands
# ---------------------------------------------------------------------------
@app.on_callback_query(filters.regex("^(settings_menu|help_menu|start_menu|create_post_help|cancel_process)$"))
async def navigation_handler(bot, cq: CallbackQuery):
    data, uid = cq.data, cq.from_user.id
    chat_id = cq.message.chat.id
    
    if data == "cancel_process" and uid in user_conversations:
        del user_conversations[uid]
    
    await cq.answer()

    if data == "cancel_process":
        await cq.message.edit_text("✅ Process cancelled.")
        return

    await cq.message.delete()

    if data == "start_menu":
        buttons = [
            [InlineKeyboardButton(" 🎬 ʜᴏᴡ ᴛᴏ ᴄʀᴇᴀᴛᴇ ᴀ ᴘᴏꜱᴛ", callback_data="create_post_help")],
            [InlineKeyboardButton("✪ ꜱᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ", url="https://t.me/MovieEntertainment4u"), InlineKeyboardButton("〄 ᴜᴘᴅᴀᴛᴇs ᴄʜᴀɴɴᴇʟ", url="https://t.me/MOVIE_LOVERZZ")],
            [InlineKeyboardButton("⚙️ ꜱᴇᴛᴛɪɴɢꜱ", callback_data="settings_menu"), InlineKeyboardButton("〆 ᴀʙᴏᴜᴛ 〆", callback_data="about_bot")],
            [InlineKeyboardButton("✧ ᴄʀᴇᴀᴛᴏʀ ✧", url="https://t.me/mladminbot")]
        ]
        await bot.send_photo(
            chat_id=chat_id,
            photo="https://envs.sh/vxr.jpg",
            caption=(
                f"👋 ᴡᴇʟᴄᴏᴍᴇ, {cq.from_user.mention}!\n\n"
                "🎬 ɪ’ᴍ ʏᴏᴜʀ **ᴀᴅᴠᴀɴᴄᴇᴅ ᴘᴏꜱᴛ ɢᴇɴᴇʀᴀᴛᴏʀ ʙᴏᴛ** — ʙᴜɪʟᴛ ᴛᴏ ᴄʀᴇᴀᴛᴇ ʙᴇᴀᴜᴛɪꜰᴜʟ ᴍᴏᴠɪᴇ & ꜱᴇʀɪᴇꜱ ᴘᴏꜱᴛꜱ ᴇꜰꜰᴏʀᴛʟᴇꜱꜱʟʏ!\n\n"
                "✨ **ʜᴇʀᴇ’ꜱ ᴡʜᴀᴛ ɪ ᴄᴀɴ ᴅᴏ ꜰᴏʀ ʏᴏᴜ:**\n"
                "1️⃣ **ꜱᴍᴀʀᴛ ᴀᴜᴛᴏ ᴘᴏꜱᴛ:**\n"
                "   ᴊᴜꜱᴛ ꜱᴇɴᴅ ᴍᴇ ᴀ ᴍᴏᴠɪᴇ ᴏʀ ᴛᴠ ꜱᴇʀɪᴇꜱ ɴᴀᴍᴇ — ɪ’ʟʟ ꜰᴇᴛᴄʜ ᴀʟʟ ᴛʜᴇ ᴅᴇᴛᴀɪʟꜱ ᴀɴᴅ ɢᴇɴᴇʀᴀᴛᴇ ᴀ ꜱᴛᴜɴɴɪɴɢ ᴘᴏꜱᴛ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ!\n\n"
                "2️⃣ **Qᴜɪᴄᴋ ᴍᴀɴᴜᴀʟ ᴘᴏꜱᴛ:**\n"
                "   ꜱᴇɴᴅ ᴍᴇ ᴀɴʏ ᴘʜᴏᴛᴏ ᴏʀ ᴠɪᴅᴇᴏ, ᴀɴᴅ ɪ’ʟʟ ʟᴇᴛ ʏᴏᴜ ᴄʜᴏᴏꜱᴇ ᴀ ᴄʜᴀɴɴᴇʟ ꜰʀᴏᴍ ʏᴏᴜʀ ʟɪꜱᴛ.\n"
                "   ɪ’ʟʟ ɪɴꜱᴛᴀɴᴛʟʏ ᴘᴏꜱᴛ ɪᴛ ᴛʜᴇʀᴇ ᴡɪᴛʜ ʏᴏᴜʀ ᴄᴜꜱᴛᴏᴍ ʜᴇᴀᴅᴇʀ, ꜰᴏᴏᴛᴇʀ, ᴄᴀᴘᴛɪᴏɴ, ʙᴜᴛᴛᴏɴꜱ, ᴀɴᴅ ʀᴇᴀᴄᴛɪᴏɴꜱ!\n\n"
                "💡 **ᴛʀʏ ɪᴛ ɴᴏᴡ:**\n"
                "ꜱᴇɴᴅ ᴀ ᴍᴏᴠɪᴇ ɴᴀᴍᴇ ᴏʀ ᴀ ᴘʜᴏᴛᴏ/ᴠɪᴅᴇᴏ ᴛᴏ ɢᴇᴛ ꜱᴛᴀʀᴛᴇᴅ!\n\n"
                "📢 Your Creative Posting Assistant 💫"
            ),
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    
    elif data == "help_menu" or data == "create_post_help":
        help_text = (
            "📚 **Help & Commands Guide**\n\n"
            "Here is a complete list of commands you can use:\n\n"
            "**╒═══「 ʙᴀꜱɪᴄ ᴄᴏᴍᴍᴀɴᴅꜱ 」**\n"
            "├ `/start` - ᴄʜᴇᴄᴋ ɪꜰ ᴛʜᴇ ʙᴏᴛ ɪꜱ ʀᴜɴɴɪɴɢ 🥳\n"
            "└ `/help` - ꜱʜᴏᴡ ᴛʜᴇ ʜᴇʟᴘ ᴍᴇɴᴜ 📚\n\n"
            
            "**╒═══「 ᴄʜᴀɴɴᴇʟ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ 」**\n"
            "├ `/addchannel` - ᴀᴅᴅ ᴀ ᴄʜᴀɴɴᴇʟ ➕\n"
            "├ `/mychannels` - ꜱᴇᴇ ʏᴏᴜʀ ꜱᴀᴠᴇᴅ ᴄʜᴀɴɴᴇʟꜱ 📂\n"
            "└ `/delchannel` - ᴅᴇʟᴇᴛᴇ ᴀ ᴄʜᴀɴɴᴇʟ 🗑\n\n"

            "**╒═══「 ᴘᴏꜱᴛ ᴄᴜꜱᴛᴏᴍɪᴢᴀᴛɪᴏɴ 」**\n"
            "├ `/setheader` - ꜱᴇᴛ ᴀ ᴄᴜꜱᴛᴏᴍ ʜᴇᴀᴅᴇʀ ✍️\n"
            "├ `/seeheader` - ᴠɪᴇᴡ ʏᴏᴜʀ ʜᴇᴀᴅᴇʀ 👀\n"
            "├ `/delheader` - ᴅᴇʟᴇᴛᴇ ʏᴏᴜʀ ʜᴇᴀᴅᴇʀ ❌\n"
            "├ `/setfooter` - ꜱᴇᴛ ᴀ ᴄᴜꜱᴛᴏᴍ ꜰᴏᴏᴛᴇʀ ✍️\n"
            "├ `/seefooter` - ᴠɪᴇᴡ ʏᴏᴜʀ ꜰᴏᴏᴛᴇʀ 👀\n"
            "├ `/delfooter` - ᴅᴇʟᴇᴛᴇ ʏᴏᴜʀ ꜰᴏᴏᴛᴇʀ ❌\n"
            "├ `/setcap` - ꜱᴇᴛ ᴀ ᴄᴜꜱᴛᴏᴍ ᴄᴀᴘᴛɪᴏɴ ✍️\n"
            "├ `/seecap` - ᴠɪᴇᴡ ʏᴏᴜʀ ᴄᴀᴘᴛɪᴏɴ 👀\n"
            "└ `/delcap` - ᴅᴇʟᴇᴛᴇ ʏᴏᴜʀ ᴄᴀᴘᴛɪᴏɴ ❌\n\n"

            "**╒═══「 ᴜʀʟ ʙᴜᴛᴛᴏɴꜱ 」**\n"
            "├ `/addbutton` - ᴀᴅᴅ ᴀ ᴄᴜꜱᴛᴏᴍ ʙᴜᴛᴛᴏɴ 🔘\n"
            "├ `/mybuttons` - ꜱᴇᴇ ʏᴏᴜʀ ʙᴜᴛᴛᴏɴꜱ 📂\n"
            "├ `/delbutton` - ᴅᴇʟᴇᴛᴇ ᴀ ʙᴜᴛᴛᴏɴ 🗑\n"
            "└ `/clearbuttons` - ᴄʟᴇᴀʀ ᴀʟʟ ʙᴜᴛᴛᴏɴꜱ ♻️\n\n"

            "**╒═══「 ᴘᴏꜱᴛᴇʀ ᴄᴜꜱᴛᴏᴍɪᴢᴀᴛɪᴏɴ 」**\n"
            "├ `/setwatermark` - ꜱᴇᴛ ᴀ ᴡᴀᴛᴇʀᴍᴀʀᴋ 💧\n"
            "├ `/delwatermark` - ʀᴇᴍᴏᴠᴇ ʏᴏᴜʀ ᴡᴀᴛᴇʀᴍᴀʀᴋ 🚫\n"
            
            "**╒═══「 ʟɪɴᴋ ꜱʜᴏʀᴛᴇɴᴇʀ 」**\n"
            "├ `/setapi` - ꜱᴇᴛ ꜱʜᴏʀᴛᴇɴᴇʀ ᴀᴘɪ ᴋᴇʏ 🔑\n"
            "├ `/delapi` - ʀᴇᴍᴏᴠᴇ ꜱʜᴏʀᴛᴇɴᴇʀ ᴀᴘɪ ❌\n"
            "├ `/setdomain` - ꜱᴇᴛ ꜱʜᴏʀᴛᴇɴᴇʀ ᴅᴏᴍᴀɪɴ 🌐\n"
            "└ `/deldomain` - ʀᴇᴍᴏᴠᴇ ꜱʜᴏʀᴛᴇɴᴇʀ ᴅᴏᴍᴀɪɴ 🚫\n\n"

            "**╒═══「 ᴏᴛʜᴇʀ ꜱᴇᴛᴛɪɴɢꜱ 」**\n"
            "├ `/settutorial` - ꜱᴇᴛ ᴅᴏᴡɴʟᴏᴀᴅ ᴛᴜᴛᴏʀɪᴀʟ ʟɪɴᴋ 🎥\n"
            "├ `/deltutorial` - ʀᴇᴍᴏᴠᴇ ᴛʜᴇ ᴛᴜᴛᴏʀɪᴀʟ ʟɪɴᴋ ❌\n"
            "└ `/settings` - ᴠɪᴇᴡ ʏᴏᴜʀ ᴄᴜʀʀᴇɴᴛ ꜱᴇᴛᴛɪɴɢꜱ ⚙️"
        )
        await bot.send_message(
            chat_id=chat_id,
            text=help_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✪ ꜱᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ ✪", url="https://t.me/movie_loverzz")],
                [InlineKeyboardButton("⌫ Back", callback_data="start_menu")]
            ])
        )

    elif data == "settings_menu":
        user_data = await users_collection.find_one({'user_id': uid}) or {}
        
        # --- Fetch and format the channel list ---
        channels = user_data.get('channels', [])
        if channels:
            # Join all channel titles into a single string, each on a new line
            channel_list_text = "\n".join([f"└ {ch['title']}" for ch in channels])
        else:
            channel_list_text = "└ Not Set"

        # Fetch all other settings with default values
        header = user_data.get('custom_header', 'Not Set')
        footer = user_data.get('custom_footer', 'Not Set')
        caption = user_data.get('custom_caption', 'Not Set')
        watermark = user_data.get('watermark_text', 'Not Set')
        api = "******" + user_data.get('shortener_api', ' ')[-4:] if user_data.get('shortener_api') else 'Not Set'
        domain = user_data.get('shortener_url', 'Not Set')
        tutorial = user_data.get('tutorial_link', 'Not Set')

        settings_text = (
            "⚙️ **Your Current Settings:**\n\n"
            
            "**╒═══「 ꜱᴀᴠᴇᴅ ᴄʜᴀɴɴᴇʟꜱ 」**\n"
            f"{channel_list_text}\n\n"

            "**╒═══「 ᴘᴏꜱᴛ ᴄᴜꜱᴛᴏᴍɪᴢᴀᴛɪᴏɴ 」**\n"
            f"├ **Header:** `{header}`\n"
            f"├ **Footer:** `{footer}`\n"
            f"└ **Extra Caption:** `{caption}`\n\n"
            
            "**╒═══「 ᴘᴏꜱᴛᴇʀ ᴄᴜꜱᴛᴏᴍɪᴢᴀᴛɪᴏɴ 」**\n"
            f"└ **Watermark:** `{watermark}`\n\n"
            
            "**╒═══「 ʟɪɴᴋ ꜱʜᴏʀᴛᴇɴᴇʀ 」**\n"
            f"├ **API Key:** `{api}`\n"
            f"└ **Domain:** `{domain}`\n\n"
            
            "**╒═══「 ᴏᴛʜᴇʀ ꜱᴇᴛᴛɪɴɢꜱ 」**\n"
            f"└ **Tutorial Link:** `{tutorial}`\n\n"
            
            "Use the commands in `/help` to change these."
        )
        await bot.send_message(
            chat_id=chat_id,
            text=settings_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✪ ꜱᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ ✪", url="https://t.me/movie_loverzz")],
                [InlineKeyboardButton("⌫ Back", callback_data="start_menu")]
            ])
                            )

@app.on_callback_query(filters.regex("cancel_process"))
async def cancel_process_handler(bot: Client, cq: CallbackQuery):
    """
    Handles the cancellation of any ongoing user process.
    Clears the user's session from user_conversations.
    """
    uid = cq.from_user.id
    
    # Check if a conversation exists and delete it
    if uid in user_conversations:
        del user_conversations[uid]
        
    # Notify the user with a pop-up alert
    await cq.answer("Process Cancelled!", show_alert=True)
    
    # Edit the message to provide clear confirmation
    await cq.message.edit_text(
        "✅ **All unfinished tasks have been cancelled.**\n\n"
        "You can now start creating a new post."
    )

# ===================================================================
# 🔹 About Bot Handler (New Separate Function)
# ===================================================================
@app.on_callback_query(filters.regex("^about_bot$"))
async def about_bot_handler(bot: Client, cq: CallbackQuery):
    await cq.answer()
    await cq.message.delete() # Deletes the previous message (the start menu)

    about_text = (
        "<b>✦✗✦ <a href='https://t.me/movie_loverzz'>ᴍy ᴅᴇᴛᴀɪʟꜱ ʙy Movie Loverz</a> ✦✗✦</b>\n\n"
        "‣ ᴍʏ ɴᴀᴍᴇ :Post_Generator\n"
        "‣ ᴍʏ ʙᴇsᴛ ғʀɪᴇɴᴅ : <a href='tg://user?id={user_id}'>ᴛʜɪs ᴘᴇʀsᴏɴ</a>\n"
        "‣ ᴅᴇᴠᴇʟᴏᴘᴇʀ : <a href='https://t.me/mladminbot'>Hawkeye</a>\n"
      #  "‣ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ : <a href='https://t.me/PrimeXBots'>ᴘʀɪᴍᴇXʙᴏᴛꜱ</a>\n"
       # "‣ ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ : <a href='https://t.me/PrimeCineZone'>Pʀɪᴍᴇ Cɪɴᴇᴢᴏɴᴇ</a>\n"
       # "‣ ѕᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ : <a href='https://t.me/Prime_Support_group'>ᴘʀɪᴍᴇ X ѕᴜᴘᴘᴏʀᴛ</a>\n"
        #"‣ ᴅᴀᴛᴀ ʙᴀsᴇ : <a href='https://www.mongodb.com/'>ᴍᴏɴɢᴏ ᴅʙ</a>\n"
        "‣ ʙᴏᴛ sᴇʀᴠᴇʀ : <a href='https://heroku.com'>ʜᴇʀᴏᴋᴜ</a>\n"
        "‣ ʙᴜɪʟᴅ sᴛᴀᴛᴜs : ᴠ2.7.1 [sᴛᴀʙʟᴇ]"
    ).format(user_id=cq.from_user.id)
    
    await bot.send_message(
        chat_id=cq.message.chat.id,
        text=about_text,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⌫ Back", callback_data="start_menu")]
        ])
    )


    
@app.on_callback_query(filters.regex("refresh_check"))
async def refresh_callback(bot, cq: CallbackQuery):
    if await is_subscribed(bot, cq.from_user.id):
        await cq.message.delete()
        # Mock a message object to call start_handler
        mock_message = type("Mock", (), {"from_user": cq.from_user, "reply_photo": cq.message.reply_photo, "reply_text": cq.message.reply_text})
        await start_handler(bot, mock_message)
    else:
        await cq.answer("❌ You still haven't joined the channel.", show_alert=True)

@app.on_message(filters.private & filters.command(["addchannel", "mychannels", "delchannel"]))
async def channel_management(bot: Client, msg: Message):
    if AUTH_CHANNEL:
        try:
            btn = await is_subscribed(client, message, AUTH_CHANNEL)
            if btn:
                username = (await client.get_me()).username
                if len(message.command) > 1:
                    btn.append([InlineKeyboardButton("♻️ ʀᴇғʀᴇsʜ ♻️", url=f"https://t.me/{username}?start={message.command[1]}")])
                else:
                    btn.append([InlineKeyboardButton("♻️ ʀᴇғʀᴇsʜ ♻️", url=f"https://t.me/{username}?start=true")])

                await message.reply_photo(
                    photo="https://i.postimg.cc/xdkd1h4m/IMG-20250715-153124-952.jpg",  # Replace with your image link
                    caption=(  
                        f"<b>👋 Hello {message.from_user.mention},\n\n"  
                        "ɪꜰ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴜꜱᴇ ᴍᴇ, ʏᴏᴜ ᴍᴜꜱᴛ ꜰɪʀꜱᴛ ᴊᴏɪɴ ᴏᴜʀ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ. "  
                        "ᴄʟɪᴄᴋ ᴏɴ \"✇ ᴊᴏɪɴ ᴏᴜʀ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ ✇\" ʙᴜᴛᴛᴏɴ.ᴛʜᴇɴ ᴄʟɪᴄᴋ ᴏɴ ᴛʜᴇ \"ʀᴇǫᴜᴇꜱᴛ ᴛᴏ ᴊᴏɪɴ\" ʙᴜᴛᴛᴏɴ. "  
                        "ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ, ᴄʟɪᴄᴋ ᴏɴ \"ʀᴇғʀᴇsʜ\" ʙᴜᴛᴛᴏɴ.</b>"  
                    ),  
                    reply_markup=InlineKeyboardMarkup(btn)
                )
                return
        except Exception as e:
            print(e)
    command, user_id = msg.command[0].lower(), msg.from_user.id
    if command == "addchannel":
        if len(msg.command) < 2: return await msg.reply_text("⚠️ **Usage:** `/addchannel [Channel ID]`")
        try: channel_id = int(msg.command[1])
        except ValueError: return await msg.reply_text("⚠️ Invalid Channel ID.")
        try:
            chat = await bot.get_chat(channel_id)
            if chat.type != enums.ChatType.CHANNEL: return await msg.reply_text("⚠️ This ID does not belong to a channel.")
            saved, status_text = await save_channel(user_id, channel_id, chat.title)
            await msg.reply_text(f"✅ {status_text}" if saved else f"⚠️ {status_text}")
        except ValueError as e: await msg.reply_text(f"❌ Error: {e}")
        except Exception as e: logger.error(e); await msg.reply_text("❌ Channel not found or I don't have access.")
    elif command == "mychannels":
        user = await users_collection.find_one({"user_id": user_id})
        if not user or not user.get("channels"): return await msg.reply_text("📂 You have no channels saved.")
        text = "📂 **Your Saved Channels:**\n" + "\n".join([f"🔹 **{ch['title']}** (`{ch['id']}`)" for ch in user["channels"]])
        await msg.reply_text(text)
    elif command == "delchannel":
        user = await users_collection.find_one({"user_id": user_id})
        if not user or not user.get("channels"): return await msg.reply_text("📂 No channels to delete.")
        buttons = [[InlineKeyboardButton(f"❌ {ch['title']}", callback_data=f"delch_{ch['id']}")] for ch in user["channels"]]
        await msg.reply_text("🗑️ Select a channel to remove:", reply_markup=InlineKeyboardMarkup(buttons))

@app.on_message(filters.private & filters.command(["setcap", "delcap", "seecap"]))
async def caption_commands(bot: Client, msg: Message):
    command, user_id = msg.command[0].lower(), msg.from_user.id
    if command == "setcap":
        caption = msg.text.split(" ", 1)[1] if len(msg.command) > 1 else None
        if not caption: return await msg.reply_text("⚠️ **Usage:** `/setcap [your caption]`\n\nTo remove your caption, use `/delcap`.")
        await users_collection.update_one({"user_id": user_id}, {"$set": {"custom_caption": caption}}, upsert=True)
        await msg.reply_text("✅ Custom caption has been set!")
    elif command == "seecap":
        user = await users_collection.find_one({"user_id": user_id})
        if not user or not user.get("custom_caption"): return await msg.reply_text("⚠️ You don't have a custom caption.")
        await msg.reply_text(f"📝 **Your current caption:**\n\n{user['custom_caption']}")
    elif command == "delcap":
        await users_collection.update_one({"user_id": user_id}, {"$unset": {"custom_caption": ""}})
        await msg.reply_text("🗑️ Custom caption has been deleted!")

@app.on_message(filters.private & filters.command(["setheader", "delheader", "seeheader", "setfooter", "delfooter", "seefooter"]))
async def header_footer_commands(bot: Client, msg: Message):
    command, user_id = msg.command[0].lower(), msg.from_user.id
    
    # Determine which field to update based on the command
    field_map = {
        "setheader": "custom_header", "delheader": "custom_header", "seeheader": "custom_header",
        "setfooter": "custom_footer", "delfooter": "custom_footer", "seefooter": "custom_footer"
    }
    field_name = field_map[command]
    field_type = "Header" if "header" in command else "Footer"

    # Set Header/Footer
    if command.startswith("set"):
        text = msg.text.split(" ", 1)[1] if len(msg.command) > 1 else None
        if not text:
            return await msg.reply_text(f"⚠️ **Usage:** `/{command} [your {field_type.lower()} text]`")
        await users_collection.update_one({"user_id": user_id}, {"$set": {field_name: text}}, upsert=True)
        await msg.reply_text(f"✅ Custom {field_type} has been set!")

    # See Header/Footer
    elif command.startswith("see"):
        user = await users_collection.find_one({"user_id": user_id})
        content = user.get(field_name) if user else None
        if not content:
            return await msg.reply_text(f"⚠️ You don't have a custom {field_type.lower()} set.")
        await msg.reply_text(f"📝 **Your current {field_type}:**\n\n{content}")

    # Delete Header/Footer
    elif command.startswith("del"):
        await users_collection.update_one({"user_id": user_id}, {"$unset": {field_name: ""}})
        await msg.reply_text(f"🗑️ Custom {field_type} has been deleted!")

@app.on_message(filters.private & filters.command(["addbutton", "mybuttons", "delbutton", "clearbuttons"]))
async def button_commands(bot: Client, msg: Message):
    command, user_id = msg.command[0].lower(), msg.from_user.id
    if command == "addbutton":
        if "|" not in msg.text: return await msg.reply_text("⚠️ **Usage:** `/addbutton Name | http://link.com`")
        try:
            text, url = [part.strip() for part in msg.text.split(" ", 1)[1].split("|", 1)]
            if not text or not url.startswith(('http://', 'https://')): raise ValueError
            await users_collection.update_one({"user_id": user_id}, {"$push": {"custom_buttons": {"text": text, "url": url}}}, upsert=True)
            await msg.reply_text(f"✅ Button **'{text}'** has been added!")
        except (ValueError, IndexError): await msg.reply_text("⚠️ Invalid format. Please use the format: `/addbutton Name | URL`")
    elif command == "mybuttons":
        user = await users_collection.find_one({"user_id": user_id})
        if not user or not user.get("custom_buttons"): return await msg.reply_text("📂 You have no custom buttons.")
        buttons = [[InlineKeyboardButton(b["text"], url=b["url"])] for b in user["custom_buttons"]]
        await msg.reply_text("📂 **Your Custom Buttons:**", reply_markup=InlineKeyboardMarkup(buttons))
    elif command == "delbutton":
        user = await users_collection.find_one({"user_id": user_id})
        if not user or not user.get("custom_buttons"): return await msg.reply_text("📂 No buttons to delete.")
        buttons = [[InlineKeyboardButton(f"❌ {b['text']}", callback_data=f"delbtn_{b['text']}")] for b in user["custom_buttons"]]
        await msg.reply_text("🗑️ Select a button to remove:", reply_markup=InlineKeyboardMarkup(buttons))
    elif command == "clearbuttons":
        await users_collection.update_one({"user_id": user_id}, {"$set": {"custom_buttons": []}})
        await msg.reply_text("🗑️ All custom buttons have been deleted!")

@app.on_message(filters.private & filters.command([
    "setwatermark", "delwatermark", "setapi", "delapi", "setdomain", "deldomain", 
    "settutorial", "deltutorial", "settings", "badge", "delbadge"
]))
async def settings_commands(bot: Client, msg: Message):
    command = msg.command[0].lower()
    user_id = msg.from_user.id
    text_parts = msg.text.split(" ", 1)
    value = text_parts[1] if len(text_parts) > 1 else None

    if command == "setwatermark":
        if value:
            await users_collection.update_one({"user_id": user_id}, {"$set": {"watermark_text": value}}, upsert=True)
            await msg.reply_text(f"✅ Watermark set to: `{value}`")
        else:
            await msg.reply_text("⚠️ **Usage:** `/setwatermark [your text]`\n\nTo remove your watermark, use `/delwatermark`.")
    
    elif command == "delwatermark":
        await users_collection.update_one({"user_id": user_id}, {"$unset": {"watermark_text": ""}})
        await msg.reply_text("🗑️ Watermark removed.")
    
    elif command == "setapi":
        if value:
            await users_collection.update_one({"user_id": user_id}, {"$set": {"shortener_api": value}}, upsert=True)
            await msg.reply_text("✅ Shortener API Key has been set.")
        else:
            await msg.reply_text("⚠️ **Usage:** `/setapi [API_KEY]`\n\nTo remove your API key, use `/delapi`.")
            
    elif command == "delapi":
        await users_collection.update_one({"user_id": user_id}, {"$unset": {"shortener_api": ""}})
        await msg.reply_text("🗑️ Shortener API Key has been removed.")

    elif command == "setdomain":
        if value:
            clean_value = value.replace("https://", "").replace("http://", "")
            await users_collection.update_one({"user_id": user_id}, {"$set": {"shortener_url": clean_value}}, upsert=True)
            await msg.reply_text(f"✅ Shortener domain set to: `{clean_value}`")
        else:
            await msg.reply_text("⚠️ **Usage:** `/setdomain [yourdomain.com]`\n\nTo remove your domain, use `/deldomain`.")
            
    elif command == "deldomain":
        await users_collection.update_one({"user_id": user_id}, {"$unset": {"shortener_url": ""}})
        await msg.reply_text("🗑️ Shortener domain has been removed.")

    elif command == "settutorial":
        if value and value.startswith(('http://', 'https://')):
            await users_collection.update_one({"user_id": user_id}, {"$set": {"tutorial_link": value}}, upsert=True)
            await msg.reply_text("✅ Tutorial link has been set.")
        else:
            await msg.reply_text("⚠️ **Usage:** `/settutorial [https://your-link.com]`\n\nTo remove the link, use `/deltutorial`.")

    elif command == "deltutorial":
        await users_collection.update_one({"user_id": user_id}, {"$unset": {"tutorial_link": ""}})
        await msg.reply_text("🗑️ Tutorial link has been deleted!")

    elif command == "badge":
        if value:
            user_conversations.setdefault(user_id, {})['temp_badge_text'] = value
            await msg.reply_text(f"✅ One-time badge for the next post set to: `{value}`.")
        else:
            await msg.reply_text("⚠️ **Usage:** `/badge [your text]`\n\nTo remove the one-time badge, use `/delbadge`.")

    elif command == "delbadge":
        user_conversations.get(user_id, {}).pop('temp_badge_text', None)
        await msg.reply_text("🗑️ One-time badge text removed.")
        
    elif command == "settings":
        # We can reuse the navigation handler for a consistent UI
        mock_message = await msg.reply_text("Loading settings...")
        mock_cq = type("Mock", (), {"data": "settings_menu", "from_user": msg.from_user, "message": mock_message, "answer": lambda: asyncio.sleep(0)})
        await navigation_handler(bot, mock_cq)
            
@app.on_message(filters.private & filters.command(["stats", "broadcast"]) & filters.user(OWNER_ID))
async def owner_commands(bot: Client, msg: Message):
    if msg.command[0].lower() == "stats":
        total_users = await users_collection.count_documents({})
        pipeline = [{"$project": {"channel_count": {"$size": {"$ifNull": ["$channels", []]}}}}]
        total_channels = sum(doc["channel_count"] async for doc in users_collection.aggregate(pipeline))
        await msg.reply_text(f"📊 **Bot Stats:**\n\n👤 **Total Users:** {total_users}\n📂 **Total Saved Channels:** {total_channels}")
    elif msg.command[0].lower() == "broadcast":
        if not msg.reply_to_message: return await msg.reply_text("⚠️ Reply to a message to broadcast.")
        sent, failed = 0, 0
        status_msg = await msg.reply_text("📢 Starting broadcast...")
        async for user_doc in users_collection.find({}, {"user_id": 1}):
            try: await msg.reply_to_message.copy(user_doc["user_id"]); sent += 1
            except Exception: failed += 1
        await status_msg.edit_text(f"✅ **Broadcast complete!**\n\n📤 **Sent:** {sent}\n❌ **Failed:** {failed}")

@app.on_callback_query(filters.regex("^(delch_|delbtn_)"))
async def delete_callback_handler(bot: Client, cq: CallbackQuery):
    user_id = cq.from_user.id
    if cq.data.startswith("delch_"):
        ch_id = int(cq.data.split("_")[1])
        await users_collection.update_one({"user_id": user_id}, {"$pull": {"channels": {"id": ch_id}}})
        await cq.answer("🗑️ Channel removed!", show_alert=True)
    elif cq.data.startswith("delbtn_"):
        text = cq.data.split("_", 1)[1]
        await users_collection.update_one({"user_id": user_id}, {"$pull": {"custom_buttons": {"text": text}}})
        await cq.answer(f"🗑️ Button '{text}' removed!", show_alert=True)
    await cq.message.delete()

# ---------------------------------------------------------------------------
# 🔹 Run The Bot
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("✅ Bot is starting...")
    app.run()
    logger.info("👋 Bot has stopped.")
