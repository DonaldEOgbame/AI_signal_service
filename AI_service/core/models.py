from django.db import models
from django.utils import timezone
import uuid

# ──────────────────────────────────────────────────────────────────────────────
# 1. Users & Subscriptions
# ──────────────────────────────────────────────────────────────────────────────

class TelegramUser(models.Model):
    """Represents a user of the bot, identified by their Telegram account."""
    telegram_id       = models.BigIntegerField(unique=True, db_index=True)
    username          = models.CharField(max_length=64, blank=True)
    device_fingerprint= models.CharField(max_length=256, blank=True)
    last_ip           = models.GenericIPAddressField(null=True, blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.username or self.telegram_id}"


class Subscription(models.Model):
    """Tracks free trials, payments, referrals, and active periods."""
    STATUS_CHOICES = [
        ('TRIAL',   'Trial'),
        ('ACTIVE',  'Active'),
        ('EXPIRED', 'Expired'),
        ('CANCELED','Canceled'),
    ]

    user              = models.OneToOneField(TelegramUser, on_delete=models.CASCADE)
    trial_start       = models.DateTimeField(default=timezone.now)
    trial_end         = models.DateTimeField()
    status            = models.CharField(max_length=10, choices=STATUS_CHOICES, default='TRIAL')
    # Payment info
    paystack_reference= models.CharField(max_length=128, blank=True)
    payment_ip        = models.GenericIPAddressField(null=True, blank=True)
    discount_applied  = models.BooleanField(default=False)  # true if paid within trial/grace
    # Referral info
    referral_code     = models.CharField(max_length=10, unique=True)
    referred_by       = models.CharField(max_length=10, blank=True)
    early_ref_count   = models.PositiveIntegerField(default=0)
    subscription_end  = models.DateTimeField(null=True, blank=True)
    # User preferences
    mode              = models.CharField(max_length=10, default='moderate')  # conservative/moderate/aggressive
    risk_pct          = models.FloatField(default=0.5)  # percent of equity per trade
    # Device/IP lock for trial
    trial_device      = models.CharField(max_length=256, blank=True)
    trial_ip          = models.GenericIPAddressField(null=True, blank=True)

    def __str__(self):
        return f"{self.user} → {self.status}"


# ──────────────────────────────────────────────────────────────────────────────
# 2. Signal Source & Quality Tracking
# ──────────────────────────────────────────────────────────────────────────────

class TelegramSource(models.Model):
    """A Telegram group/channel we monitor for signals."""
    chat_id           = models.BigIntegerField(unique=True, db_index=True)
    title             = models.CharField(max_length=200)
    reputation        = models.FloatField(default=50.0)    # aggregated from backtests & feedback
    message_count     = models.PositiveIntegerField(default=0)
    signal_count      = models.PositiveIntegerField(default=0)
    last_quality      = models.FloatField(default=0.0)     # signal_count/message_count
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-reputation']

    def __str__(self):
        return self.title


# ──────────────────────────────────────────────────────────────────────────────
# 3. Raw Messages & Parsed Signals
# ──────────────────────────────────────────────────────────────────────────────

class RawMessage(models.Model):
    """Every raw Telegram message + OCR text for audit and reprocessing."""
    source            = models.ForeignKey(TelegramSource, on_delete=models.CASCADE)
    telegram_msg_id   = models.CharField(max_length=64, unique=True)
    text              = models.TextField()
    image             = models.ImageField(null=True, blank=True)
    ocr_text          = models.TextField(null=True, blank=True)
    received_at       = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.source}#{self.telegram_msg_id}"


class Signal(models.Model):
    """A fully parsed, provider-style trading signal."""
    EXIT_ORIGINS = [
        ('EXPLICIT','Explicit'),
        ('INFERRED','Inferred'),
        ('UPDATED','Updated'),
    ]

    id                = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_message       = models.ForeignKey(RawMessage, on_delete=models.CASCADE)
    asset             = models.CharField(max_length=20, db_index=True)  # e.g. BTC, ETH
    action            = models.CharField(max_length=10)                 # BUY, SELL, SHORT
    entry_price       = models.FloatField()
    stop_loss         = models.FloatField()
    target_prices     = models.JSONField(default=list, blank=True)      # [TP1, TP2, …]
    timeframe         = models.CharField(max_length=10)                 # 1H, 4H, Daily
    trade_type        = models.CharField(max_length=20)                 # BREAKOUT, SCALP, SWING…
    confidence        = models.FloatField(db_index=True)               # 0–100%
    exit_origin       = models.CharField(max_length=10, choices=EXIT_ORIGINS, default='EXPLICIT')
    is_conflict       = models.BooleanField(default=False)
    conflict_winner   = models.BooleanField(null=True)                 # True if this signal won
    conflict_reason   = models.TextField(blank=True)
    reason            = models.TextField(blank=True)                   # one-line rationale
    scored_at         = models.DateTimeField(auto_now_add=True)
    timestamp_utc     = models.DateTimeField(db_index=True)
    expires_at_utc    = models.DateTimeField(db_index=True)
    dedup_key         = models.CharField(max_length=128, blank=True, db_index=True)

    class Meta:
        ordering = ['-timestamp_utc']

    def __str__(self):
        return f"{self.asset} {self.action}@{self.entry_price}"


# ──────────────────────────────────────────────────────────────────────────────
# 4. Trade Orders & Execution Logging
# ──────────────────────────────────────────────────────────────────────────────

class TradeOrder(models.Model):
    """An MT5 order placed (or pending) in response to a Signal."""
    STATUS_CHOICES = [
        ('PENDING','Pending'),
        ('EXECUTED','Executed'),
        ('REJECTED','Rejected'),
        ('FAILED','Failed'),
    ]

    user              = models.ForeignKey(TelegramUser, on_delete=models.CASCADE)
    signal            = models.OneToOneField(Signal, on_delete=models.CASCADE)
    size              = models.FloatField()   # lots or units
    status            = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    expected_slippage = models.FloatField(null=True, blank=True)
    actual_slippage   = models.FloatField(null=True, blank=True)
    slippage_cost     = models.FloatField(null=True, blank=True)
    placed_at         = models.DateTimeField(null=True, blank=True)
    executed_at       = models.DateTimeField(null=True, blank=True)
    profit_loss       = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"Order[{self.status}] {self.signal}"


class ExecutionAttempt(models.Model):
    """Detailed log of each API call for placing/modifying an order."""
    trade_order       = models.ForeignKey(TradeOrder, on_delete=models.CASCADE, related_name='attempts')
    attempt_number    = models.PositiveIntegerField()
    request_payload   = models.JSONField(default=dict, blank=True)
    response_data     = models.JSONField(default=dict, blank=True)
    duration_ms       = models.IntegerField(null=True, blank=True)
    success           = models.BooleanField(default=False)
    error_message     = models.TextField(null=True, blank=True)
    attempted_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('trade_order','attempt_number')]


# ──────────────────────────────────────────────────────────────────────────────
# 5. Feedback & Reputation
# ──────────────────────────────────────────────────────────────────────────────

class Feedback(models.Model):
    """User feedback on signals to refine reputation and NLP accuracy."""
    user              = models.ForeignKey(TelegramUser, on_delete=models.CASCADE)
    signal            = models.ForeignKey(Signal, on_delete=models.CASCADE, related_name='feedbacks')
    useful            = models.BooleanField()
    corrected_fields  = models.JSONField(default=dict, blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['signal','useful'])]


class BacktestResult(models.Model):
    """Aggregated performance metrics of a source over a time window."""
    source            = models.ForeignKey(TelegramSource, on_delete=models.CASCADE)
    period_start      = models.DateField()
    period_end        = models.DateField()
    hit_rate          = models.FloatField()
    avg_risk_reward   = models.FloatField()
    expectancy        = models.FloatField()
    drawdown          = models.FloatField()
    computed_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('source','period_start','period_end')]


# ──────────────────────────────────────────────────────────────────────────────
# 6. Broadcasts & Summaries
# ──────────────────────────────────────────────────────────────────────────────

class Broadcast(models.Model):
    """Custom messages you send to segments of users."""
    TARGET_CHOICES = [
        ('ALL',    'All Users'),
        ('ACTIVE', 'Active Subscribers'),
        ('TRIAL',  'Trial Users'),
        ('EXPIRED','Expired'),
    ]
    sender           = models.ForeignKey(TelegramUser, on_delete=models.SET_NULL, null=True,
                                         related_name='sent_broadcasts')
    text             = models.TextField()
    target_segment   = models.CharField(max_length=10, choices=TARGET_CHOICES, default='ALL')
    created_at       = models.DateTimeField(auto_now_add=True)


class SummaryReport(models.Model):
    """AI-generated narrative summaries for a user and period."""
    TONE_CHOICES = [
        ('humorous','Humorous'),
        ('serious',  'Serious'),
    ]
    user              = models.ForeignKey(TelegramUser, on_delete=models.CASCADE)
    period_type       = models.CharField(max_length=10)       # day, week, month, quarter, year
    period_start      = models.DateField()
    period_end        = models.DateField()
    tone              = models.CharField(max_length=10, choices=TONE_CHOICES)
    summary_text      = models.TextField()
    generated_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-generated_at']


# ──────────────────────────────────────────────────────────────────────────────
# 7. Analytics & Device Abuse Control
# ──────────────────────────────────────────────────────────────────────────────

class AnalyticsEvent(models.Model):
    """Low-level events for monitoring latency, volumes, and errors."""
    EVENT_TYPES = [
        ('INGEST','Ingest'),('PARSE','Parse'),
        ('ALERT','Alert'),('EXEC','Execute'),
        ('FEEDBACK','Feedback'),
    ]
    user              = models.ForeignKey(TelegramUser, on_delete=models.SET_NULL, null=True, blank=True)
    signal            = models.ForeignKey(Signal, on_delete=models.SET_NULL, null=True, blank=True)
    event_type        = models.CharField(max_length=10, choices=EVENT_TYPES)
    duration_ms       = models.IntegerField(null=True, blank=True)
    metadata          = models.JSONField(default=dict, blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)


class DeviceBlock(models.Model):
    """Records device/IP that have abused trials or referrals."""
    telegram_user     = models.ForeignKey(TelegramUser, on_delete=models.CASCADE, related_name='blocks')
    device_fingerprint= models.CharField(max_length=256)
    ip_address        = models.GenericIPAddressField()
    reason            = models.CharField(max_length=200)
    blocked_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('device_fingerprint','ip_address')]

