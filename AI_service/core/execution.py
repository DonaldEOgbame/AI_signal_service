import asyncio
import requests
import MetaTrader5 as mt5
from datetime import datetime
from django.conf import settings
from cryptography.fernet import Fernet
from .models import TradeOrder, ExecutionAttempt, Subscription

_user_locks = {}
_fernet = Fernet(settings.MT5_ENC_KEY.encode())


def _decrypt(value: str) -> str:
    try:
        return _fernet.decrypt(value.encode()).decode()
    except Exception:
        return value

async def execute_order(user, sig, size, request):
    """Execute an MT5 order for a specific user with per-user locking."""
    lock = _user_locks.setdefault(user.id, asyncio.Lock())
    async with lock:
        sub = Subscription.objects.get(user=user)
        start = datetime.utcnow()
        result_data = {}
        comment = ''
        retcode = -1
        if settings.EXECUTOR_URL:
            url = settings.EXECUTOR_URL.rstrip('/') + '/order'
            payload = {
                'server': _decrypt(sub.mt5_server),
                'login': _decrypt(sub.mt5_login),
                'password': _decrypt(sub.mt5_password),
                'request': request,
            }
            headers = {}
            if settings.EXECUTOR_TOKEN:
                headers['Authorization'] = f'Bearer {settings.EXECUTOR_TOKEN}'
            loop = asyncio.get_running_loop()
            def _post():
                return requests.post(url, json=payload, headers=headers, timeout=15)
            resp = await loop.run_in_executor(None, _post)
            try:
                result_data = resp.json()
                retcode = result_data.get('retcode', -1)
                comment = result_data.get('comment', '')
            except Exception:
                comment = 'executor error'
        else:
            loop = asyncio.get_running_loop()
            def _send():
                if not mt5.initialize(server=_decrypt(sub.mt5_server), login=int(_decrypt(sub.mt5_login)), password=_decrypt(sub.mt5_password)):
                    return {'retcode': -1, 'comment': 'init failed'}
                try:
                    if 'price' not in request:
                        tick = mt5.symbol_info_tick(request['symbol'])
                        if request['type'] == mt5.ORDER_TYPE_BUY:
                            request['price'] = tick.ask
                        else:
                            request['price'] = tick.bid
                    res = mt5.order_send(request)
                    return res._asdict()
                finally:
                    mt5.shutdown()
            result_data = await loop.run_in_executor(None, _send)
            retcode = result_data.get('retcode', -1)
            comment = result_data.get('comment', '')
        end = datetime.utcnow()
        status = 'EXECUTED' if retcode == mt5.TRADE_RETCODE_DONE else 'FAILED'
        order = TradeOrder.objects.create(
            user=user,
            signal=sig,
            size=size,
            status=status,
            placed_at=start,
            executed_at=end,
            profit_loss=0.0,
        )
        ExecutionAttempt.objects.create(
            trade_order=order,
            attempt_number=1,
            request_payload=request,
            response_data=result_data,
            duration_ms=int((end-start).total_seconds()*1000),
            success=status == 'EXECUTED',
            error_message=comment,
        )
        return retcode, comment
