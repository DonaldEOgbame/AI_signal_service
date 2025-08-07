import os
import json
import asyncio
import logging
import uuid
import re
import base64
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
from langdetect import detect
from googletrans import Translator
from django.db.models import Sum, Avg, F, ExpressionWrapper, DurationField

from core.ui import EMOJI, color_text

from core.models import (
    TelegramUser, Subscription, TelegramSource, RawMessage, Signal,
    TradeOrder, ExecutionAttempt, Feedback, BacktestResult,
    Broadcast, SummaryReport, DeviceBlock, AnalyticsEvent, UserSource
)

logger = logging.getLogger(__name__)

translator = Translator()

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
ADMIN_ID      = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
TRIAL_DAYS    = 7
GRACE_HOURS   = 24
MAX_MSG_RATE  = 30  # msgs/sec

# Scheduler
sched = AsyncIOScheduler(timezone="UTC")

# runtime state containers
setup_state = {}
correction_state = {}
admin_state = {}
pending_force = {}

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


def encrypt(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def decrypt(text: str) -> str:
    try:
        return base64.b64decode(text.encode()).decode()
    except Exception:
        return text


def translate_to_en(text: str) -> str:
    try:
        lang = detect(text)
        if lang != 'en':
            return translator.translate(text, src=lang, dest='en').text
    except Exception:
        return text
    return text


signal_regex = re.compile(r"(?P<action>buy|sell)\s+(?P<asset>[A-Za-z0-9]+).*?(?P<entry>\d+(?:\.\d+)?).*?sl[:\s]*(?P<sl>\d+(?:\.\d+)?)", re.I)


def regex_parse(text: str):
    match = signal_regex.search(text)
    if not match:
        return {}
    d = match.groupdict()
    return {
        'asset': d['asset'].upper(),
        'action': d['action'].upper(),
        'entry': float(d['entry']),
        'stop_loss': float(d['sl'])
    }

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


def fetch_high_impact_events(symbol, window_minutes):
    """Placeholder for economic calendar API."""
    return []


def compute_atr(symbol, periods=14):
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, periods + 1)
    if rates is None or len(rates) < periods + 1:
        return 0.0
    trs = []
    for i in range(1, len(rates)):
        high = rates[i]['high'] if isinstance(rates[i], dict) else rates[i][2]
        low = rates[i]['low'] if isinstance(rates[i], dict) else rates[i][3]
        prev_close = rates[i-1]['close'] if isinstance(rates[i-1], dict) else rates[i-1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs)

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

async def scheduled_summaries(target_user=None, period=None, tone=None):
    now = timezone.now()
    periods = [
        ('day', timedelta(days=1)),
        ('week', timedelta(weeks=1)),
        ('month', timedelta(days=30)),
        ('quarter', timedelta(days=90)),
        ('year', timedelta(days=365)),
    ]
    users = [target_user] if target_user else TelegramUser.objects.all()
    for p, delta in periods:
        if period and p != period:
            continue
        start = now - delta
        for u in users:
            orders = TradeOrder.objects.filter(
                user=u, status='EXECUTED',
                executed_at__range=(start, now)
            )
            if not orders:
                continue
            total = sum(o.profit_loss for o in orders)
            wr = orders.filter(profit_loss__gt=0).count()/orders.count()*100
            best = max(orders, key=lambda o: o.profit_loss)
            worst= min(orders, key=lambda o: o.profit_loss)
            actual_tone = tone or ('humorous' if u.subscription.mode=='aggressive' else 'serious')
            prompt = (
                f"Write a {actual_tone} {p} summary: {len(orders)} trades, P/L {total:.2f}%, "
                f"win-rate {wr:.1f}%, best {best.signal.asset}{best.signal.action}+{best.profit_loss:.2f}%, "
                f"worst {worst.signal.asset}{worst.signal.action}{worst.profit_loss:.2f}%."
            )
            ai = openai.Completion.create(
                engine="gpt-4", prompt=prompt,
                max_tokens=200, temperature=0.7
            ).choices[0].text.strip()
            SummaryReport.objects.create(
                user=u, period_type=p,
                period_start=start.date(), period_end=now.date(),
                tone=actual_tone,
                summary_text=ai
            )
            await send(u.telegram_id, f"🗓️ {p.capitalize()} Summary:\n{ai}")


async def daily_fraud_report():
    since = timezone.now() - timedelta(hours=24)
    blocks = DeviceBlock.objects.filter(blocked_at__gte=since)
    if not blocks:
        return
    lines = [f"{b.telegram_user} – {b.reason}" for b in blocks]
    await send(ADMIN_ID, "Fraud Report:\n" + "\n".join(lines))

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
    sched.add_job(lambda: asyncio.create_task(send(uid, '⌛ Trial reminder: 4 days left.')),
                  'date', run_date=timezone.now()+timedelta(days=3))
    sched.add_job(lambda: asyncio.create_task(send(uid, '⌛ Trial nearly over!')),
                  'date', run_date=timezone.now()+timedelta(days=6))
    sched.add_job(lambda: asyncio.create_task(send(uid, '🚫 Trial ended. Subscribe to continue.')),
                  'date', run_date=timezone.now()+timedelta(days=7))
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


@client.on(events.NewMessage(pattern='/setup'))
async def cmd_setup(evt):
    """Interactive setup flow for channel and MT5 credentials."""
    uid = evt.sender_id
    u, _ = TelegramUser.objects.get_or_create(
        telegram_id=uid,
        defaults={'username': evt.sender.username or ''}
    )
    setup_state[uid] = {'step': 'channel', 'user': u}
    await send(uid, 'Forward a message from the channel you want monitored.')

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
    u = TelegramUser.objects.get(telegram_id=evt.sender_id)
    sub = Subscription.objects.get(user=u)
    orders = TradeOrder.objects.filter(user=u).select_related('signal').order_by('-placed_at')[:10]

    total_signals = Signal.objects.filter(raw_message__source__subscribed_by=u).count()
    executed = TradeOrder.objects.filter(user=u, status='EXECUTED').count()

    account = mt5.account_info()
    today = timezone.now().date()
    today_pl = (
        TradeOrder.objects.filter(user=u, executed_at__date=today)
        .aggregate(sum=Sum('profit_loss'))['sum'] or 0
    )
    equity_change = 0.0
    if account and account.equity:
        equity_change = (today_pl / account.equity) * 100

    avg_dur = (
        TradeOrder.objects.filter(user=u, executed_at__isnull=False, placed_at__isnull=False)
        .annotate(dur=ExpressionWrapper(F('executed_at') - F('placed_at'), output_field=DurationField()))
        .aggregate(avg=Avg('dur'))['avg']
    )
    if avg_dur:
        total_sec = int(avg_dur.total_seconds())
        hours, minutes = divmod(total_sec // 60, 60)
        avg_hold = f"{hours}h {minutes}m"
    else:
        avg_hold = "0m"

    top = (
        TradeOrder.objects.filter(user=u, status='EXECUTED')
        .values('signal__asset').annotate(pl=Sum('profit_loss'))
        .order_by('-pl').first()
    )
    top_symbol = f"{top['signal__asset']} ({top['pl']:+.1f}%) {EMOJI['spark']}" if top else 'N/A'

    text = (
        f"📊 Status:\nSubscription: {sub.status}\n"
        f"Trial ends: {sub.trial_end.date()}\n"
        f"Mode: {sub.mode}, Risk: {sub.risk_pct}%\n"
        f"Signals Parsed: {total_signals} | Trades Executed: {executed}\n"
        f"📈 Equity Change Today: {equity_change:+.1f}%\n"
        f"🕒 Avg Hold Time: {avg_hold}\n"
        f"🏆 Top Symbol: {top_symbol}\n"
        f"📜 Trade History:\n"
    )
    for o in orders:
        pl = o.profit_loss or 0
        icon = 'green' if pl >= 0 else 'red'
        entry_ts = o.placed_at.strftime('%Y-%m-%d %H:%M') if o.placed_at else '—'
        exit_ts = o.executed_at.strftime('%Y-%m-%d %H:%M') if o.executed_at else '—'
        line = f"{o.signal.asset} {o.signal.action} @{o.signal.entry_price} → {pl:+.1f}% ({entry_ts} → {exit_ts})"
        text += color_text(line, icon) + "\n"
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
        text+=f"{EMOJI['spark']} {r.source.title}: Exp {r.expectancy:.2f}% | Hit {r.hit_rate:.0f}%\n"
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
    parts = evt.raw_text.split()
    if len(parts) < 2 or parts[1] not in ['day', 'week', 'month', 'quarter', 'year']:
        return await send(evt.sender_id, "Usage: /summary <period> [humorous|serious]")
    period = parts[1]
    tone = parts[2] if len(parts) > 2 else None
    u = TelegramUser.objects.get(telegram_id=evt.sender_id)
    await scheduled_summaries(target_user=u, period=period, tone=tone)

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


@client.on(events.NewMessage(pattern='/admin'))
async def cmd_admin(evt):
    if evt.sender_id != ADMIN_ID:
        return
    buttons = [Button.inline('Broadcast', b'adm_bc'),
               Button.inline('View Stats', b'adm_stats')]
    await send(evt.sender_id, 'Admin:', buttons)


@client.on(events.NewMessage(pattern='/channels'))
async def cmd_channels(evt):
    parts = evt.raw_text.split()
    u = TelegramUser.objects.get(telegram_id=evt.sender_id)
    if len(parts) == 1:
        subs = u.sources.all()
        if not subs:
            return await send(evt.sender_id, "No channel subscriptions.")
        lines = [f"{s.id}: {s.title}" for s in subs]
        msg = "Your channels:\n" + "\n".join(lines)
        msg += "\nUse /channels remove <id> to unsubscribe."
        await send(evt.sender_id, msg)
    elif len(parts) == 3 and parts[1] == 'remove':
        try:
            src = TelegramSource.objects.get(id=int(parts[2]))
            src.subscribed_by.remove(u)
            await send(evt.sender_id, f"Removed {src.title}.")
        except (TelegramSource.DoesNotExist, ValueError):
            await send(evt.sender_id, "Invalid channel id.")
    else:
        await send(evt.sender_id, "Usage: /channels [remove <id>]")


@client.on(events.NewMessage(pattern='/backtest'))
async def cmd_backtest(evt):
    await send(evt.sender_id, "Backtesting coming soon!")


@client.on(events.NewMessage(func=lambda e: e.is_private and not e.raw_text.startswith('/')))
async def handle_private(evt):
    uid = evt.sender_id
    # setup flow
    if uid in setup_state:
        state = setup_state[uid]
        u = state['user']
        if state['step'] == 'channel':
            if evt.fwd_from and evt.fwd_from.channel_id:
                chat = await evt.forward.get_chat() if evt.forward else await evt.get_chat()
                src, _ = TelegramSource.objects.get_or_create(
                    chat_id=evt.fwd_from.channel_id,
                    defaults={'title': getattr(chat, 'title', 'Channel')}
                )
                UserSource.objects.get_or_create(user=u, source=src)
                setup_state[uid]['step'] = 'mt5'
                await send(uid, 'Send MT5 server login password separated by spaces.')
            else:
                await send(uid, 'Please forward a message from the channel.')
        elif state['step'] == 'mt5':
            parts = evt.raw_text.split()
            if len(parts) != 3:
                await send(uid, 'Format: <server> <login> <password>')
            else:
                sub = Subscription.objects.get(user=u)
                sub.mt5_server = encrypt(parts[0])
                sub.mt5_login = encrypt(parts[1])
                sub.mt5_password = encrypt(parts[2])
                sub.save()
                await send(uid, 'Setup complete.')
                del setup_state[uid]
        return
    # correction flow
    if uid in correction_state:
        sig_id = correction_state.pop(uid)
        try:
            field, value = evt.raw_text.split('=',1)
            sig = Signal.objects.get(id=sig_id)
            setattr(sig, field.strip(), value.strip())
            sig.save()
            await send(uid, 'Signal updated.')
        except Exception:
            await send(uid, 'Format should be field=value')
        return
    # admin broadcast text
    if uid in admin_state and admin_state[uid]=='broadcast':
        Broadcast.objects.create(
            sender=TelegramUser.objects.filter(telegram_id=uid).first(),
            text=evt.raw_text
        )
        await send(uid, 'Broadcast queued.')
        del admin_state[uid]

# ──────────────────────────────────────────────────────────────────────────────
# Message Listener for Signal Channels
# ──────────────────────────────────────────────────────────────────────────────

# @client.on(events.NewMessage)
async def legacy_all_message(evt):
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


@client.on(events.NewMessage(func=lambda e: e.is_group or e.is_channel))
async def all_message(evt):
    if settings.BOT_PAUSED:
        return
    chat_id = evt.chat_id
    try:
        src = TelegramSource.objects.get(chat_id=chat_id)
    except TelegramSource.DoesNotExist:
        return
    rm = RawMessage.objects.create(
        source=src,
        telegram_msg_id=str(evt.id),
        text=evt.raw_text or "",
    )
    if isinstance(evt.media, MessageMediaPhoto):
        path = await evt.download_media()
        ocr = ocr_extract(path)
        rm.ocr_text = ocr
        rm.save()
    blob = rm.text + "\n" + (rm.ocr_text or "")
    blob = translate_to_en(blob)
    parsed = regex_parse(blob) or nlp_parse(blob)
    if not parsed.get('asset'):
        return
    upd = re.search(r'update\s+sl\s+to\s+(\d+(?:\.\d+)?)', blob, re.I)
    if upd:
        new_sl = float(upd.group(1))
        orders = TradeOrder.objects.filter(signal__asset__iexact=parsed['asset'], status='EXECUTED')
        for o in orders:
            try:
                mt5.order_send({'action': mt5.TRADE_ACTION_SLTP, 'symbol': o.signal.asset, 'sl': new_sl})
                o.signal.stop_loss = new_sl
                o.signal.exit_origin = 'UPDATED'
                o.signal.save()
            except Exception:
                continue
        return
    key = f"{parsed['asset']}{parsed['action']}{parsed['entry']}{parsed['stop_loss']}"
    recent = Signal.objects.filter(dedup_key=key, timestamp_utc__gte=timezone.now()-timedelta(seconds=30))
    if recent.exists():
        return
    recent_asset = Signal.objects.filter(
        asset=parsed['asset'], timestamp_utc__gte=timezone.now()-timedelta(minutes=2)
    )
    conflict, prev = False, None
    if recent_asset.exists() and recent_asset[0].action != parsed['action']:
        conflict, prev = True, recent_asset[0]
    winner, reason = None, ""
    if conflict:
        snap = {}
        pick, reason = select_conflict(parsed, {
            'asset': prev.asset, 'action': prev.action,
            'entry': prev.entry_price, 'stop_loss': prev.stop_loss
        }, snap)
        if pick == '2':
            return
    sig = Signal.objects.create(
        raw_message=rm,
        asset=parsed['asset'], action=parsed['action'],
        entry_price=parsed['entry'], stop_loss=parsed['stop_loss'],
        target_prices=parsed.get('targets', []),
        timeframe=parsed.get('timeframe', ''),
        trade_type=parsed.get('trade_type', ''),
        confidence=parsed.get('confidence', 0),
        exit_origin=parsed.get('exit_origin', 'EXPLICIT'),
        is_conflict=conflict, conflict_winner=(not conflict or True),
        conflict_reason=reason, reason=parsed.get('reason', ''),
        timestamp_utc=timezone.now(),
        expires_at_utc=timezone.now() + timedelta(minutes=30),
        dedup_key=key
    )
    buttons = [
        Button.inline("✅ Approve", f"approve:{sig.id}"),
        Button.inline("❌ Reject", f"reject:{sig.id}"),
        Button.inline("👍 Useful", f"useful:{sig.id}"),
        Button.inline("👎 Not", f"notuse:{sig.id}"),
        Button.inline("✏️ Correct", f"correct:{sig.id}")
    ]
    card = (
        f"🔔 Signal {sig.id}\n"
        f"{sig.asset} ▶️ {sig.action}\n"
        f"Entry: {sig.entry_price}\nSL: {sig.stop_loss}\n"
        f"TP: {','.join(map(str, sig.target_prices))}\n"
        f"Conf: {sig.confidence}%"
    )
    for u in src.subscribed_by.all():
        sub = Subscription.objects.get(user=u)
        now = timezone.now()
        if sub.status == 'TRIAL' and now > sub.trial_end + timedelta(hours=GRACE_HOURS):
            await send(u.telegram_id, "🚫 Trial expired. Subscribe to continue.")
            continue
        if sub.trading_start and sub.trading_end and not (sub.trading_start <= now.time() <= sub.trading_end):
            continue
        if sub.allowed_weekdays and now.weekday() not in sub.allowed_weekdays:
            continue
        if sub.allowed_symbols and parsed['asset'] not in sub.allowed_symbols:
            continue
        if sub.blocked_symbols and parsed['asset'] in sub.blocked_symbols:
            continue
        tick = mt5.symbol_info_tick(parsed['asset'])
        if tick and sub.min_volume and getattr(tick, 'volume', 0) < sub.min_volume:
            continue
        size = calculate_size(sub, parsed['entry'], parsed['stop_loss'])
        # High impact news guardrail
        if sub.avoid_high_impact:
            events = fetch_high_impact_events(parsed['asset'], sub.news_window_minutes)
            if events:
                if sub.mode != 'aggressive':
                    await send(u.telegram_id, f"{EMOJI['news']} Trade skipped due to high-impact news.")
                    continue
                pending_force[(u.telegram_id, str(sig.id))] = (size, sig)
                btns = [
                    Button.inline("Force Trade", f"force:{sig.id}:{u.telegram_id}"),
                    Button.inline("Skip", f"skip:{sig.id}:{u.telegram_id}")
                ]
                await send(u.telegram_id, f"{EMOJI['news']}{EMOJI['warning']} High-impact news detected. Proceed?", btns)
                continue
        # ATR volatility filter
        if sub.vol_threshold:
            atr = compute_atr(parsed['asset'])
            price = tick.ask if parsed['action'] == 'BUY' else tick.bid
            atr_ratio = atr / price if price else 0
            if atr_ratio > sub.vol_threshold:
                if sub.mode != 'aggressive':
                    await send(u.telegram_id, f"{EMOJI['volatility']} Trade skipped due to high volatility.")
                    continue
                pending_force[(u.telegram_id, str(sig.id))] = (size, sig)
                btns = [
                    Button.inline("Force Trade", f"force:{sig.id}:{u.telegram_id}"),
                    Button.inline("Skip", f"skip:{sig.id}:{u.telegram_id}")
                ]
                await send(u.telegram_id, f"{EMOJI['volatility']}{EMOJI['warning']} Volatility high. Force trade?", btns)
                continue
        await send(u.telegram_id, card + f"\nSize: {size:.4f}", buttons)
        if sub.mode == 'aggressive':
            await handle_execution(u, sig, size)
    if src.last_quality < 0.1:
        for u in src.subscribed_by.all():
            msg = f"{src.title} is {100*(1-src.last_quality):.0f}% noise—unsubscribe?"
            btns = [Button.inline('Yes', f'unsub:{src.id}'), Button.inline('No', f'keep:{src.id}')]
            await send(u.telegram_id, msg, btns)

@client.on(events.CallbackQuery)
async def callback(evt):
    data = evt.data.decode()
    uid = evt.sender_id
    if ':' in data:
        action, ident = data.split(':', 1)
    else:
        action, ident = data, None
    if action in {'approve', 'reject', 'useful', 'notuse', 'correct'} and ident:
        u = TelegramUser.objects.get(telegram_id=uid)
        sig = Signal.objects.get(id=ident)
        if action == 'approve':
            size = calculate_size(u.subscription, sig.entry_price, sig.stop_loss)
            await handle_execution(u, sig, size)
            await evt.answer('Order approved & placed.')
        elif action == 'reject':
            await evt.answer('Signal rejected.')
        elif action == 'useful':
            Feedback.objects.create(user=u, signal=sig, useful=True)
            await evt.answer('Marked useful.')
        elif action == 'notuse':
            Feedback.objects.create(user=u, signal=sig, useful=False)
            await evt.answer('Marked not useful.')
        elif action == 'correct':
            correction_state[uid] = sig.id
            await evt.answer('Send field=value')
    elif action == 'adm_bc':
        admin_state[uid] = 'broadcast'
        await evt.answer('Send broadcast text')
    elif action == 'adm_stats':
        count = TelegramUser.objects.count()
        await send(uid, f'Users: {count}')
        await evt.answer()
    elif action == 'unsub' and ident:
        src = TelegramSource.objects.get(id=int(ident))
        u = TelegramUser.objects.get(telegram_id=uid)
        src.subscribed_by.remove(u)
        await evt.answer('Unsubscribed')
    elif action == 'keep':
        await evt.answer('Kept')
    elif action == 'force' and ident:
        sig_id, uid_str = ident.split(':')
        key = (int(uid_str), sig_id)
        if key in pending_force:
            size, sig = pending_force.pop(key)
            user = TelegramUser.objects.get(telegram_id=int(uid_str))
            await handle_execution(user, sig, size)
        await evt.answer('Order forced.')
    elif action == 'skip' and ident:
        sig_id, uid_str = ident.split(':')
        pending_force.pop((int(uid_str), sig_id), None)
        await evt.answer('Trade skipped.')

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
    msg = (
        f"{EMOJI['check']} Order Executed: {sig.asset} {sig.action} {size:.2f} @{price}"
        if status == "EXECUTED"
        else f"{EMOJI['red']} Order Failed: {err}"
    )
    await send(user.telegram_id, msg)

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
        sched.add_job(lambda: asyncio.create_task(daily_fraud_report()),
                      'cron', hour=0, minute=0)
        sched.start()
        # Run Telethon event loop
        client.run_until_disconnected()
