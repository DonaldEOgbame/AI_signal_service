from django.db import migrations
from django.conf import settings
import base64
from cryptography.fernet import Fernet


def forwards(apps, schema_editor):
    Subscription = apps.get_model('core', 'Subscription')
    f = Fernet(settings.MT5_ENC_KEY.encode())
    for sub in Subscription.objects.all():
        updated = False
        for field in ['mt5_server', 'mt5_login', 'mt5_password']:
            value = getattr(sub, field)
            if not value:
                continue
            try:
                decoded = base64.b64decode(value).decode()
            except Exception:
                decoded = value
            encrypted = f.encrypt(decoded.encode()).decode()
            setattr(sub, field, encrypted)
            updated = True
        if updated:
            sub.save(update_fields=['mt5_server', 'mt5_login', 'mt5_password'])


def backwards(apps, schema_editor):
    Subscription = apps.get_model('core', 'Subscription')
    f = Fernet(settings.MT5_ENC_KEY.encode())
    for sub in Subscription.objects.all():
        for field in ['mt5_server', 'mt5_login', 'mt5_password']:
            value = getattr(sub, field)
            if not value:
                continue
            try:
                decrypted = f.decrypt(value.encode()).decode()
            except Exception:
                decrypted = value
            encoded = base64.b64encode(decrypted.encode()).decode()
            setattr(sub, field, encoded)
        sub.save(update_fields=['mt5_server', 'mt5_login', 'mt5_password'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_alter_subscription_news_window_minutes_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
