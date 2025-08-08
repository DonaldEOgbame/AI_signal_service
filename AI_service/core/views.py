import os
import json
import logging
from datetime import timedelta
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings

from .models import Subscription, Broadcast, TelegramUser

logger = logging.getLogger(__name__)

# Load your admin broadcast token from env
ADMIN_BROADCAST_TOKEN = os.getenv("ADMIN_BROADCAST_TOKEN", "")

# ──────────────────────────────────────────────────────────────────────────────
# 1. Paystack Webhook
# ──────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def paystack_webhook(request):
    """
    Handle Paystack payment.success events to activate subscriptions:
    - Only count as 'discount' if paid <= trial_end + 24h
    - Else activate at standard price
    """
    try:
        payload = json.loads(request.body)
        event   = payload.get("event")
        data    = payload.get("data", {})
        ref     = data.get("reference")
        ip      = request.META.get("REMOTE_ADDR")
    except json.JSONDecodeError:
        logger.exception("Invalid JSON in Paystack webhook")
        return HttpResponse(status=400)

    if event != "charge.success" or not ref:
        return HttpResponse(status=200)

    try:
        sub = Subscription.objects.get(paystack_reference=ref)
    except Subscription.DoesNotExist:
        logger.warning(f"No subscription found for Paystack ref {ref}")
        return HttpResponse(status=404)

    now = timezone.now()
    # Determine if early-bird discount applies
    if now <= sub.trial_end + timedelta(hours=24):
        sub.discount_applied = True
    sub.status           = "ACTIVE"
    sub.payment_ip       = ip
    # Extend subscription_end by 30 days (or from now if none)
    start = sub.subscription_end if sub.subscription_end and sub.subscription_end > now else now
    sub.subscription_end = start + timedelta(days=30)
    sub.save()

    # Check referrals
    if sub.referred_by:
        try:
            referrer = Subscription.objects.get(referral_code=sub.referred_by)
            # only count if discount_applied
            if sub.discount_applied:
                referrer.early_ref_count += 1
                if referrer.early_ref_count >= 2:
                    # grant a free month
                    ref_start = referrer.subscription_end if referrer.subscription_end and referrer.subscription_end > now else now
                    referrer.subscription_end = ref_start + timedelta(days=30)
                    referrer.early_ref_count  = 0
                    # notify referrer via bot (handled in runbot via AnalyticsEvent or direct Telethon call)
                referrer.save()
        except Subscription.DoesNotExist:
            logger.warning(f"Invalid referred_by code: {sub.referred_by}")

    return HttpResponse(status=200)

# ──────────────────────────────────────────────────────────────────────────────
# 2. Health Check
# ──────────────────────────────────────────────────────────────────────────────

def health(request):
    """
    Basic health check for:
      - Database connectivity
      - (Optionally) Payment integration (just checks SECRET exists)
    """
    status = {
        "db":          "ok",
        "paystack_ok": bool(os.getenv("PAYSTACK_SECRET_KEY")),
        "mt5_ok":      Subscription.objects.filter(mt5_server__gt="", mt5_login__gt="", mt5_password__gt="").exists(),
        "vision_ok":   bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS")),
        "openai_ok":   bool(os.getenv("OPENAI_API_KEY")),
    }
    return JsonResponse(status)

# ──────────────────────────────────────────────────────────────────────────────
# 3. Admin Broadcast Creation
# ──────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def create_broadcast(request):
    """
    Admin-only endpoint to schedule a broadcast.
    Expects JSON:
      {
        "text": "Your message here",
        "target_segment": "ALL" | "ACTIVE" | "TRIAL" | "EXPIRED"
      }
    Header must include:
      Authorization: Bearer <ADMIN_BROADCAST_TOKEN>
    """
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {ADMIN_BROADCAST_TOKEN}":
        return HttpResponseForbidden("Invalid admin token")

    try:
        data = json.loads(request.body)
        text   = data["text"]
        target = data["target_segment"]
        if target not in dict(Broadcast.TARGET_CHOICES):
            return JsonResponse({"error": "Invalid target_segment"}, status=400)
    except (json.JSONDecodeError, KeyError) as e:
        return JsonResponse({"error": "Malformed request body"}, status=400)

    # Persist the broadcast; runbot will pick it up and deliver
    bc = Broadcast.objects.create(
        sender=None,
        text=text,
        target_segment=target
    )
    return JsonResponse({
        "status": "scheduled",
        "broadcast_id": bc.id
    }, status=201)
