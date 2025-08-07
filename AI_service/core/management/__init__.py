import os
import json
import asyncio
import logging
import uuid
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.conf import settings

from telethon import TelegramClient, events, Button, utils
from telethon.tl.types import MessageMediaPhoto

import MetaTrader5 as mt5
from google.cloud import vision
import openai
import requests

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.models import (
    TelegramUser, Subscription, TelegramSource, RawMessage, Signal,
    TradeOrder, ExecutionAttempt, Feedback, BacktestResult,
    Broadcast, SummaryReport, DeviceBlock, AnalyticsEvent
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# ENV VARS & Constants
# ──────────────────────────────────────────────────────────────────────────────
API_ID        = int(os.getenv("TELEGRAM_API_ID"))
API_HASH      = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
PAYSTACK_KEY  = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_URL  = "https://api.paystack.co/transaction/initialize"
GOOGLE_CREDS  = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
OPENAI_KEY    = os.getenv("OPENAI_API_KEY")
MT5_SERVER    = os.getenv("MT5_SERVER")
MT5_LOGIN     = int(os.getenv("MT5_LOGIN"))
MT5_PASSWORD  = os.getenv("MT5_PASSWORD")
ADMIN_TOKEN   = os.getenv("ADMIN_BROADCAST_TOKEN")
TRIAL_DAYS    = 7
GRACE_HOURS   = 24
MAX_MSG_RATE  = 30  # msgs/sec

# Scheduler
sched = AsyncIOScheduler(timezone="UTC")

# ──────────────────────────────────────────────────────────────────────────────
# CLIENT INITIALIZATION
# ──────────────────────────────────────────────────────────────────────────────
# Google Vision
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_CREDS
vision_client = vision.ImageAnnotatorClient()

# OpenAI
openai.api_key = OPENAI_KEY

# MT5
mt5.initialize(server=MT5_SERVER, login=MT5_LOGIN, password=MT5_PASSWORD)

# Telethon
client = TelegramClient('session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ──────────────────────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────

async def send(user_id, text, buttons=None):
    """Send DM to a user with optional inline buttons."""
    await client.send_message(user_id, text, buttons=buttons or [])

def record_event(user, signal=None, event_type="INGEST", duration_ms=None, metadata=None):
    AnalyticsEvent.objects.create(
        user=user, signal=signal,
        event_type=event_type, duration_ms=duration_ms or 0,
        metadata=metadata or {}
    )

def ocr_extract(image_path):
    with open(image_path, 'rb') as f:
        img = vision.Image(content=f.read())
    res = vision_client.document_text_detection(image=img)
    return res.full_text_annotation.text or ""

def nlp_parse(text):
    prompt = (
        "Extract JSON {asset, action, entry, stop_loss, targets, timeframe, trade_type} "
        f"from:\n\"\"\"{text}\"\"\""
    )
    resp = openai.Completion.create(
        engine="gpt-4", prompt=prompt,
        max_tokens=150, temperature=0
    )
    try:
        return json.loads(resp.choices[0].text.strip())
    except:
        return {}

def select_conflict(sig1, sig2, price_snapshot):
    prompt = (
        "Two conflicting signals:\n"
        f"1: {sig1}\n2: {sig2}\n"
        f"Price data: {price_snapshot}\n"
        "Which is more likely accurate? Respond with 1 or 2 and a one-sentence rationale."
    )
    resp = openai.Completion.create(
        engine="gpt-4", prompt=prompt,
        max_tokens=100, temperature=0
    ).choices[0].text.strip().split("\n")[0]
    pick = '1' if resp.startswith('1') else '2'
    reason = resp[2:].strip()
    return pick, reason

def calculate_size(sub, entry, stop):
    # fetch MT5 equity
    account_info = mt5.account_info()
    equity = account_info.equity if account_info else 0
    raw = (equity * (sub.risk_pct/100)) / abs(entry - stop)
    mod = {'conservative':0.5,'moderate':1.0,'aggressive':1.5}[sub.mode]
    return raw * mod

def init_paystack_checkout(user):
    sub = user.subscription
    callback = f"{settings.SITE_URL}/api/paystack/webhook/"
    data = {
        "email": f"{user.telegram_id}@telegram.bot",
        "amount": int( sub.discount_applied and 300 or 500 ) * 100,
        "reference": str(uuid.uuid4()),
        "callback_url": callback
    }
    headers = {"Authorization": f"Bearer {PAYSTACK_KEY}"}
    res = requests.post(PAYSTACK_URL, json=data, headers=headers).json()
    if res.get("status"):
        sub.paystack_reference = data["reference"]
        sub.save()
        return res["data"]["authorization_url"]
    return None

# ──────────────────────────────────────────────────────────────────────────────
# SCHEDULED JOBS
# ──────────────────────────────────────────────────────────────────────────────

async def dispatch_broadcasts():
    for bc in Broadcast.objects.all():
        qs = TelegramUser.objects.all()
        if bc.target_segment == 'ACTIVE':
            qs = qs.filter(subscription__status='ACTIVE')
        elif bc.target_segment == 'TRIAL':
            qs = qs.filter(subscription__status='TRIAL')
        elif bc.target_segment == 'EXPIRED':
            qs = qs.filter(subscription__status='EXPIRED')
        for u in qs:
            await send(u.telegram_id, bc.text)
            await asyncio.sleep(1/MAX_MSG_RATE)
        bc.delete()

async def scheduled_summaries():
    now = timezone.now()
    periods = [
        ('day', timedelta(days=1)),
        ('week', timedelta(weeks=1)),
        ('month', timedelta(days=30)),
        ('quarter', timedelta(days=90)),
        ('year', timedelta(days=365)),
    ]
    for period, delta in periods:
        start = now - delta
        for u in TelegramUser.objects.all():
            orders = TradeOrder.objects.filter(
                user=u, status='EXECUTED',
                executed_at__range=(start, now)
            )
            if not orders: continue
            total = sum(o.profit_loss for o in orders)
            wr = orders.filter(profit_loss__gt=0).count()/orders.count()*100
            best = max(orders, key=lambda o: o.profit_loss)
            worst= min(orders, key=lambda o: o.profit_loss)
            prompt = (
                f"Write a {'humorous' if u.subscription.mode=='aggressive' else 'serious'} "
                f"{period} summary: {len(orders)} trades, P/L {total:.2f}%, "
                f"win-rate {wr:.1f}%, best {best.signal.asset}{best.signal.action}+{best.profit_loss:.2f}%, "
                f"worst {worst.signal.asset}{worst.signal.action}{worst.profit_loss:.2f}%."
            )
            ai = openai.Completion.create(
                engine="gpt-4", prompt=prompt,
                max_tokens=200, temperature=0.7
            ).choices[0].text.strip()
            SummaryReport.objects.create(
                user=u, period_type=period,
                period_start=start.date(), period_end=now.date(),
                tone='humorous' if u.subscription.mode=='aggressive' else 'serious',
                summary_text=ai
            )
            await send(u.telegram_id, f"🗓️ {period.capitalize()} Summary:\n{ai}")

# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(evt):
    uid = evt.sender_id
    u, _ = TelegramUser.objects.get_or_create(
        telegram_id=uid,
        defaults={'username':evt.sender.username or ''}
    )
    # prevent trial abuse
    if Subscription.objects.filter(user=u).exists():
        await send(uid, "🚫 You’ve already used your trial. Subscribe with /subscribe.")
        return
    sub = Subscription.objects.create(
        user=u,
        trial_start=timezone.now(),
        trial_end=timezone.now()+timedelta(days=TRIAL_DAYS),
        referral_code=str(uuid.uuid4())[:8]
    )
    await send(uid,
        "🤖 Welcome! Your 7-day trial begins now.\n"
        "Use /help to see available commands."
    )

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(evt):
    text = (
        "📋 Commands:\n"
        "/setup – configure channels & MT5\n"
        "/subscribe – start/extend subscription\n"
        "/mode <con|mod|agg> – set risk mode\n"
        "/risk <pct> – set risk %\n"
        "/status – view status\n"
        "/top channels [period] – leaderboards\n"
        "/pause /resume – stop/start bot\n"
        "/referral – your referral link\n"
        "/summary [period] [humorous|serious]\n"
        "/flag <@user> – flag abuse\n"
        "/health – system status\n"
        "/backtest – Coming Soon!"
    )
    await send(evt.sender_id, text)

@client.on(events.NewMessage(pattern='/mode'))
async def cmd_mode(evt):
    parts = evt.raw_text.split()
    if len(parts)!=2 or parts[1] not in ['con','mod','agg']:
        return await send(evt.sender_id,
            "Usage: /mode <con|mod|agg> → conservative, moderate, aggressive.")
    umap={'con':'conservative','mod':'moderate','agg':'aggressive'}
    u = TelegramUser.objects.get(telegram_id=evt.sender_id)
    sub= Subscription.objects.get(user=u)
    sub.mode = umap[parts[1]]; sub.save()
    await send(evt.sender_id, f"Mode set to {sub.mode.title()}.")

@client.on(events.NewMessage(pattern='/risk'))
async def cmd_risk(evt):
    try:
        pct = float(evt.raw_text.split()[1])
    except:
        return await send(evt.sender_id, "Usage: /risk <percent>, e.g. /risk 0.5")
    u=TelegramUser.objects.get(telegram_id=evt.sender_id)
    sub=Subscription.objects.get(user=u)
    sub.risk_pct = pct; sub.save()
    await send(evt.sender_id, f"Risk per trade set to {pct}% of equity.")

@client.on(events.NewMessage(pattern='/subscribe'))
async def cmd_subscribe(evt):
    u=TelegramUser.objects.get(telegram_id=evt.sender_id)
    link=init_paystack_checkout(u)
    if link:
        await send(evt.sender_id, f"💳 Subscribe here: {link}")
    else:
        await send(evt.sender_id, "❌ Failed to generate payment link, try later.")

@client.on(events.NewMessage(pattern='/referral'))
async def cmd_referral(evt):
    u=TelegramUser.objects.get(telegram_id=evt.sender_id)
    sub=Subscription.objects.get(user=u)
    link=f"https://t.me/{(await client.get_me()).username}?start={sub.referral_code}"
    await send(evt.sender_id, f"🎁 Referral link: {link}\nRefer 2 early payers → +30d free!")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(evt):
    u=TelegramUser.objects.get(telegram_id=evt.sender_id)
    sub=Subscription.objects.get(user=u)
    orders=TradeOrder.objects.filter(user=u).order_by('-placed_at')[:5]
    text=(
        f"📊 Status:\nSubscription: {sub.status}\n"
        f"Trial ends: {sub.trial_end.date()}\n"
        f"Mode: {sub.mode}, Risk: {sub.risk_pct}%\n"
        f"Last 5 Orders:\n"
    )
    for o in orders:
        text+=f"- {o.signal.asset}{o.signal.action}@{o.signal.entry_price} → {o.status}\n"
    await send(evt.sender_id, text)

@client.on(events.NewMessage(pattern='/top'))
async def cmd_top(evt):
    period = evt.raw_text.split()[2] if len(evt.raw_text.split())>2 else 'month'
    now=timezone.now().date()
    delta={'week':7,'month':30,'6m':180,'year':365}.get(period,30)
    start=now-timedelta(days=delta)
    rows=BacktestResult.objects.filter(
        period_start=start, period_end=now
    ).order_by('-expectancy')[:5]
    text=f"🏆 Top Channels ({period}):\n"
    for r in rows:
        text+=f"{r.source.title}: Exp {r.expectancy:.2f}% | Hit {r.hit_rate:.0f}%\n"
    await send(evt.sender_id, text)

@client.on(events.NewMessage(pattern='/pause'))
async def cmd_pause(evt):
    # set a global flag in settings or DB
    settings.BOT_PAUSED = True
    await send(evt.sender_id, "⏸️ Bot paused. Use /resume to continue.")

@client.on(events.NewMessage(pattern='/resume'))
async def cmd_resume(evt):
    settings.BOT_PAUSED = False
    await send(evt.sender_id, "▶️ Bot resumed.")

@client.on(events.NewMessage(pattern='/health'))
async def cmd_health(evt):
    res = {
        "telegram": "ok",
        "mt5":     "ok" if mt5.initialize() else "fail",
        "vision":  "ok" if vision_client else "fail",
        "openai":  "ok" if openai.api_key else "fail",
    }
    await send(evt.sender_id, json.dumps(res, indent=2))

@client.on(events.NewMessage(pattern='/summary'))
async def cmd_summary(evt):
    parts=evt.raw_text.split()
    if len(parts)<2 or parts[1] not in ['day','week','month','quarter','year']:
        return await send(evt.sender_id, "Usage: /summary <period> [humorous|serious]")
    period, tone = parts[1], parts[2] if len(parts)>2 else 'humorous'
    # trigger immediate summary job for this user only
    await scheduled_summaries()  # will DM to all; you could filter by user here

@client.on(events.NewMessage(pattern='/flag'))
async def cmd_flag(evt):
    parts=evt.raw_text.split()
    if len(parts)!=2 or not parts[1].startswith('@'):
        return await send(evt.sender_id, "Usage: /flag @username")
    target_usr = parts[1][1:]
    try:
        tu=TelegramUser.objects.get(username=target_usr)
        DeviceBlock.objects.create(
            telegram_user=tu,
            device_fingerprint=tu.device_fingerprint,
            ip_address=tu.last_ip,
            reason="Flagged by admin"
        )
        await send(evt.sender_id, f"🚫 {parts[1]} has been paused for review.")
    except TelegramUser.DoesNotExist:
        await send(evt.sender_id, "User not found.")

# ──────────────────────────────────────────────────────────────────────────────
# Message Listener for Signal Channels
# ──────────────────────────────────────────────────────────────────────────────

@client.on(events.NewMessage)
async def all_message(evt):
    if settings.BOT_PAUSED: return
    chat_id = evt.chat_id
    try:
        src = TelegramSource.objects.get(chat_id=chat_id)
    except TelegramSource.DoesNotExist:
        return
    # raw capture
    rm = RawMessage.objects.create(
        source=src,
        telegram_msg_id=str(evt.id),
        text=evt.raw_text or ""
    )
    # OCR if photo
    if isinstance(evt.media, MessageMediaPhoto):
        path = await evt.download_media()
        ocr = ocr_extract(path)
        rm.ocr_text = ocr; rm.save()
    # NLP parse
    blob = rm.text + "\n" + (rm.ocr_text or "")
    parsed = nlp_parse(blob)
    if not parsed.get("asset"): return
    # dedupe
    key = f"{parsed['asset']}{parsed['action']}{parsed['entry']}{parsed['stop_loss']}"
    recent = Signal.objects.filter(dedup_key=key, timestamp_utc__gte=timezone.now()-timedelta(seconds=30))
    if recent.exists(): return
    # conflict
    recent_asset = Signal.objects.filter(
        asset=parsed['asset'], timestamp_utc__gte=timezone.now()-timedelta(minutes=2)
    )
    conflict, prev = False, None
    if recent_asset.exists() and recent_asset[0].action!=parsed['action']:
        conflict, prev = True, recent_asset[0]
    # size & trial check
    u = TelegramUser.objects.get(telegram_id=evt.sender_id)
    sub= Subscription.objects.get(user=u)
    if sub.status=='TRIAL' and timezone.now()>sub.trial_end+timedelta(hours=GRACE_HOURS):
        await send(u.telegram_id, "🚫 Trial expired. Subscribe to continue.")
        return
    size = calculate_size(sub, parsed['entry'], parsed['stop_loss'])
    # conflict resolution
    winner, reason = None, ""
    if conflict:
        snap = {}  # you’d fetch price snapshot here
        pick, reason = select_conflict(parsed, {
            'asset':prev.asset, 'action':prev.action,
            'entry':prev.entry_price, 'stop_loss':prev.stop_loss
        }, snap)
        if pick=='2': 
            # skip this new one
            await send(u.telegram_id, f"⚠️ Conflict: choosing previous signal.\n{reason}")
            return
    # store signal
    sig = Signal.objects.create(
        raw_message=rm,
        asset=parsed['asset'], action=parsed['action'],
        entry_price=parsed['entry'], stop_loss=parsed['stop_loss'],
        target_prices=parsed.get('targets',[]),
        timeframe=parsed.get('timeframe',''),
        trade_type=parsed.get('trade_type',''),
        confidence=parsed.get('confidence',0),
        exit_origin=parsed.get('exit_origin','EXPLICIT'),
        is_conflict=conflict, conflict_winner=(not conflict or True),
        conflict_reason=reason, reason=parsed.get('reason',''),
        timestamp_utc=timezone.now(),
        expires_at_utc=timezone.now()+timedelta(minutes=30),
        dedup_key=key
    )
    # DM signal card
    buttons = [Button.inline("✅ Approve", b"approve"+sig.id.bytes),
               Button.inline("❌ Reject", b"reject"+sig.id.bytes),
               Button.inline("👍 Useful", b"useful"+sig.id.bytes),
               Button.inline("👎 Not", b"not"+sig.id.bytes)]
    card = (
        f"🔔 Signal {sig.id}\n"
        f"{sig.asset} ▶️ {sig.action}\n"
        f"Entry: {sig.entry_price}\nSL: {sig.stop_loss}\n"
        f"TP: {','.join(map(str,sig.target_prices))}\n"
        f"Size: {size:.4f}\nConf: {sig.confidence}%\n"
        f"Mode: {sub.mode.title()}"
    )
    await send(u.telegram_id, card, buttons)
    # auto-exec if aggressive
    if sub.mode=='aggressive':
        await handle_execution(u, sig, size)

@client.on(events.CallbackQuery)
async def callback(evt):
    data = evt.data.decode()
    uid  = evt.sender_id
    u    = TelegramUser.objects.get(telegram_id=uid)
    # parse action + signal id
    action = data[:7]
    sig_id = uuid.UUID(bytes=evt.data[7:])
    sig    = Signal.objects.get(id=sig_id)
    if action=="approve":
        size = calculate_size(u.subscription, sig.entry_price, sig.stop_loss)
        await handle_execution(u, sig, size)
        await evt.answer("Order approved & placed.")
    elif action=="reject":
        await evt.answer("Signal rejected.")
    elif action=="useful":
        Feedback.objects.create(user=u, signal=sig, useful=True)
        await evt.answer("Marked useful.")
    elif action=="notuse":
        Feedback.objects.create(user=u, signal=sig, useful=False)
        await evt.answer("Marked not useful.")

async def handle_execution(user, sig, size):
    # MT5 order logic
    slippage = 0.0
    order_type = mt5.ORDER_TYPE_BUY if sig.action=="BUY" else mt5.ORDER_TYPE_SELL
    price = mt5.symbol_info_tick(sig.asset).ask if sig.action=="BUY" else mt5.symbol_info_tick(sig.asset).bid
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": sig.asset,
        "volume": size,
        "type": order_type,
        "price": price,
        "sl": sig.stop_loss,
        "tp": sig.target_prices[0] if sig.target_prices else 0,
        "deviation": 10,
        "magic": 234000,
        "comment": f"SignalBot {sig.id}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(req)
    to = datetime.now()
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        status="FAILED"
        err = result.comment
    else:
        status="EXECUTED"
        err = ""
    to2 = datetime.now()
    totd = int((to2-to).total_seconds()*1000)
    tobj = TradeOrder.objects.create(
        user=user, signal=sig, size=size,
        status=status, placed_at=to,
        executed_at=to2, actual_slippage=slippage,
        profit_loss=0.0
    )
    ExecutionAttempt.objects.create(
        trade_order=tobj, attempt_number=1,
        request_payload=req, response_data=result._asdict(),
        duration_ms=totd, success=(status=="EXECUTED"), error_message=err
    )

# ──────────────────────────────────────────────────────────────────────────────
# RUNBOT COMMAND
# ──────────────────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = "Run the full Telegram Signal Parser & MT5 Trader bot"

    def handle(self, *args, **kwargs):
        # Schedule summaries & broadcasts
        sched.add_job(lambda: asyncio.create_task(scheduled_summaries()),
                      'cron', hour=23, minute=50)
        sched.add_job(lambda: asyncio.create_task(dispatch_broadcasts()),
                      'interval', seconds=60)
        sched.start()
        # Run Telethon event loop
        client.run_until_disconnected()
