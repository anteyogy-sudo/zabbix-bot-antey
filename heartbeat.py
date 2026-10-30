"""Dead man's switch (heartbeat).

Если бот падает без graceful shutdown, сервис типа healthchecks.io
отправит email, что бот перестал стучаться. Без этого при падении бота
никто не узнает, что он молчит.
"""

import logging

import requests

logger = logging.getLogger(__name__)


def ping(url, timeout=15):
    """Стучится в healthchecks.io (или совместимый сервис).

    Возвращает True при статусе 2xx/3xx, False при любой ошибке.
    Ошибки логируются, но не бросают исключений, чтобы не ломать основной цикл.
    """
    if not url:
        return True
    try:
        response = requests.get(url, timeout=timeout)
        return response.status_code < 400
    except requests.RequestException as exc:
        logger.warning("Heartbeat ping failed: %s", exc)
        return False
