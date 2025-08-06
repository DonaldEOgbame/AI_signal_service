from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
import uuid

User = get_user_model()


# ──────────────────────────────────────────────────────────────────────────────
# 0. Broker Accounts & Alert Channels
# ──────────────────────────────────────────────────────────────────────────────

class BrokerAccount(models.Model):
    """Represents a user’s connection to a broker (e.g. MT4, MT5)."""
    BROKER_CHOICES = [
        ('MT4', 'MetaTrader 4'),
        ('MT5', 'MetaTrader 5'),
        ('OTHER', 'Other'),
    ]
    user            = models.ForeignKey(User, on_delete=models.CASCADE, related_name='broker_accounts')
    name            = models.CharField(max_length=100)  # e.g. "Live Account", "Demo #1"
    broker          = models.CharField(max_length=10, choices=BROKER_CHOICES)
    account_id      = models.CharField(max_length=100)  # broker-specific identifier
    is_default      = models.BooleanField(default=False)
    credentials     = models.JSONField(blank=True, default=dict)  # connection info securely stored/encrypted elsewhere
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'name')]
        indexes = [
            models.Index(fields=['user', 'broker']),
        ]


class AlertChannel(models.Model):
    """Defines where to send alerts (email, webhook, etc.)."""
    CHANNEL_TYPES = [
        ('EMAIL', 'Email'),
        ('WEBHOOK', 'Webhook'),
        ('SLACK', 'Slack'),
        # add more as needed
    ]
    user            = models.ForeignKey(User, on_delete=models.CASCADE, related_name='alert_channels')
    channel_type    = models.CharField(max_length=20, choices=CHANNEL_TYPES)
    config          = models.JSONField(default=dict, blank=True)  # e.g. { "email": "...@..." } or webhook URL
    is_primary      = models.BooleanField(default=False)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'channel_type', 'config')]


# ──────────────────────────────────────────────────────────────────────────────
# 1. Signal ingestion & multi-modal parsing
# ──────────────────────────────────────────────────────────────────────────────

class TelegramSource(models.Model):
    name             = models.CharField(max_length=100, unique=True)
    reputation_score = models.FloatField(default=50.0)
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['-reputation_score'])]


class RawMessage(models.Model):
    source      = models.ForeignKey(TelegramSource, on_delete=models.CASCADE)
    telegram_id = models.CharField(max_length=64, unique=True)
    text        = models.TextField()
    image       = models.ImageField(null=True, blank=True)
    ocr_text    = models.TextField(null=True, blank=True)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['-ingested_at'])]


# ──────────────────────────────────────────────────────────────────────────────
# 2. Standardized Signal object & consensus/conflict logic
# ──────────────────────────────────────────────────────────────────────────────

class Signal(models.Model):
    id                = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_message       = models.ForeignKey(RawMessage, on_delete=models.CASCADE)
    asset             = models.CharField(max_length=20, db_index=True)
    action            = models.CharField(max_length=10)
    trade_type        = models.CharField(max_length=20)
    entry_price       = models.FloatField()
    entry_range_min   = models.FloatField(null=True, blank=True)
    entry_range_max   = models.FloatField(null=True, blank=True)
    target_price      = models.FloatField()
    target_levels     = models.JSONField(default=list, blank=True)
    stop_loss         = models.FloatField()
    risk_reward       = models.CharField(max_length=10)
    timeframe         = models.CharField(max_length=10)
    confidence        = models.FloatField(db_index=True)
    score             = models.FloatField(db_index=True)
    is_consensus      = models.BooleanField(default=False, db_index=True)
    conflict_with     = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='conflicted_signals'
    )
    reason            = models.TextField()
    exit_origin       = models.CharField(
        max_length=20,
        choices=[('EXPLICIT','Explicit'),('INFERRED','Inferred'),('UPDATED','Updated')],
        default='EXPLICIT'
    )
    timestamp_utc     = models.DateTimeField(db_index=True)
    expires_at_utc    = models.DateTimeField(db_index=True)
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-timestamp_utc']
        indexes = [
            models.Index(fields=['asset', 'action', 'timestamp_utc']),
            models.Index(fields=['-score']),
        ]


class SignalSource(models.Model):
    signal            = models.ForeignKey(Signal, on_delete=models.CASCADE, related_name='sources')
    source            = models.ForeignKey(TelegramSource, on_delete=models.CASCADE)
    source_confidence = models.FloatField()
    reputation_at_time= models.FloatField()

    class Meta:
        unique_together = [('signal', 'source')]


# ──────────────────────────────────────────────────────────────────────────────
# 3. Subscription & personalization
# ──────────────────────────────────────────────────────────────────────────────

class Subscription(models.Model):
    user               = models.ForeignKey(User, on_delete=models.CASCADE)
    assets             = models.JSONField(default=list, blank=True)
    sources_whitelist  = models.ManyToManyField(TelegramSource, blank=True, related_name='+')
    sources_blacklist  = models.ManyToManyField(TelegramSource, blank=True, related_name='+')
    trade_types        = models.JSONField(default=list, blank=True)
    min_confidence     = models.FloatField(default=0.0)
    require_consensus  = models.PositiveIntegerField(default=0)
    alert_channels     = models.ManyToManyField(AlertChannel, blank=True)
    updated_at         = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user',)]


class Mode(models.Model):
    name                = models.CharField(max_length=30, unique=True)
    risk_per_trade      = models.FloatField()    # fraction of equity
    size_modifier       = models.FloatField()
    min_confidence      = models.FloatField()
    require_explicit    = models.BooleanField()
    flip_delta_required = models.FloatField()
    streak_scaling      = models.BooleanField(default=False)
    drawdown_safety     = models.FloatField(default=0.0)
    created_at          = models.DateTimeField(auto_now_add=True)
    updated_at          = models.DateTimeField(auto_now=True)


class UserProfile(models.Model):
    user              = models.OneToOneField(User, on_delete=models.CASCADE)
    mode              = models.ForeignKey(Mode, on_delete=models.SET_NULL, null=True)
    email_confirm     = models.BooleanField(default=True)
    badge_preferences = models.JSONField(default=dict, blank=True)
    ui_defaults       = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=['user'])]


# ──────────────────────────────────────────────────────────────────────────────
# 4. Risk rules & execution tracking
# ──────────────────────────────────────────────────────────────────────────────

class TradeOrder(models.Model):
    signal              = models.OneToOneField(Signal, on_delete=models.SET_NULL, null=True, blank=True)
    user                = models.ForeignKey(User, on_delete=models.CASCADE)
    account             = models.ForeignKey(BrokerAccount, on_delete=models.CASCADE)
    mode                = models.ForeignKey(Mode, on_delete=models.SET_NULL, null=True)
    size                = models.FloatField()
    expected_slippage   = models.FloatField(null=True, blank=True)
    actual_slippage     = models.FloatField(null=True, blank=True)
    slippage_cost       = models.FloatField(null=True, blank=True)
    placed_at           = models.DateTimeField(null=True, blank=True)
    executed_at         = models.DateTimeField(null=True, blank=True)
    status              = models.CharField(
        max_length=20,
        choices=[('PENDING','Pending'),('EXECUTED','Executed'),('REJECTED','Rejected'),('FAILED','Failed')]
    )
    last_error          = models.TextField(null=True, blank=True)
    profit_loss         = models.FloatField(null=True, blank=True)
    created_at          = models.DateTimeField(auto_now_add=True)
    updated_at          = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['account', 'status']),
        ]


class ExecutionAttempt(models.Model):
    """Logs each attempt to execute or modify an order."""
    trade_order     = models.ForeignKey(TradeOrder, on_delete=models.CASCADE, related_name='attempts')
    attempt_number  = models.PositiveIntegerField(default=1)
    request_payload = models.JSONField(default=dict, blank=True)
    response_data   = models.JSONField(default=dict, blank=True)
    duration_ms     = models.IntegerField(null=True, blank=True)
    success         = models.BooleanField(default=False)
    error_message   = models.TextField(null=True, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('trade_order', 'attempt_number')]


class AccountStatus(models.Model):
    user            = models.ForeignKey(User, on_delete=models.CASCADE)
    account         = models.ForeignKey(BrokerAccount, on_delete=models.CASCADE)
    equity          = models.FloatField()
    free_margin     = models.FloatField()
    margin_level    = models.FloatField()
    fetched_at      = models.DateTimeField(auto_now=True)

    class Meta:
        get_latest_by = 'fetched_at'
        indexes = [
            models.Index(fields=['account', 'fetched_at']),
        ]


# ──────────────────────────────────────────────────────────────────────────────
# 5. Feedback, backtest & reputation
# ──────────────────────────────────────────────────────────────────────────────

class SignalFeedback(models.Model):
    user             = models.ForeignKey(User, on_delete=models.CASCADE)
    signal           = models.ForeignKey(Signal, on_delete=models.CASCADE, related_name='feedbacks')
    useful           = models.BooleanField()
    corrected_fields = models.JSONField(default=dict, blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['signal', 'useful']),
        ]


class BacktestResult(models.Model):
    source           = models.ForeignKey(TelegramSource, on_delete=models.CASCADE)
    account          = models.ForeignKey(BrokerAccount, on_delete=models.CASCADE)
    start_date       = models.DateField()
    end_date         = models.DateField()
    hit_rate         = models.FloatField()
    avg_risk_reward  = models.FloatField()
    expectancy       = models.FloatField()
    drawdown         = models.FloatField()
    generated_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('source', 'account', 'start_date', 'end_date')]


# ──────────────────────────────────────────────────────────────────────────────
# 6. Plugin Rules & Analytics
# ──────────────────────────────────────────────────────────────────────────────

class CustomRule(models.Model):
    """User-defined signal routing or alerting logic."""
    RULE_TYPES = [
        ('WEBHOOK', 'Webhook'),
        ('EMAIL',   'Email'),
        ('SLACK',   'Slack'),
    ]
    user            = models.ForeignKey(User, on_delete=models.CASCADE, related_name='custom_rules')
    name            = models.CharField(max_length=100)
    rule_type       = models.CharField(max_length=20, choices=RULE_TYPES)
    definition      = models.JSONField()  # e.g. {"if": {...}, "then": {...}}
    is_active       = models.BooleanField(default=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'name')]


class AnalyticsEvent(models.Model):
    EVENT_TYPES = [
        ('INGEST','Ingest'),('PARSE','Parse'),
        ('ALERT','Alert'),('EXEC','Execute'),
        ('FEEDBACK','Feedback'),
    ]
    event_type  = models.CharField(max_length=20, choices=EVENT_TYPES)
    signal      = models.ForeignKey(Signal, on_delete=models.SET_NULL, null=True, blank=True)
    user        = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    metadata    = models.JSONField(default=dict, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['event_type', 'created_at']),
        ]
