from django.contrib import admin
from django.urls import path
from core.views import paystack_webhook, health, create_broadcast

urlpatterns = [
    path('admin/', admin.site.urls),
    # Paystack payment webhook
    path('api/paystack/webhook/', paystack_webhook, name='paystack-webhook'),
    # Health check endpoint
    path('api/health/', health, name='health'),
    # Admin broadcast scheduling (secured by ADMIN_BROADCAST_TOKEN)
    path('api/admin/broadcast/', create_broadcast, name='create-broadcast'),
]
