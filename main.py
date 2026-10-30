"""zabbix_bot: опрос Zabbix API и уведомление о проблемах на почту.

Запуск:
    python main.py                 # основной цикл
    python main.py --once          # один цикл и выход (для отладки/cron)
    python main.py --dry-run       # писать в лог вместо отправки писем
    python main.py --test-mail     # проверить SMTP и выйти
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time

from config import ConfigError, describe, get_config
from heartbeat import ping as heartbeat_ping
from notifier import (
    NotificationError,
    EmailNotifier,
    build_problem_message,
    build_recovery_message,
)
from state import KIND_RECOVERY, KIND_RENOTIFY, StateStore
from zabbix_client import ZabbixAPIError, ZabbixClient, ZabbixConnectionError

logger = logging.getLogger("zabbix_bot")

_shutdown = threading.Event()



def _install_signal_handlers():
    """Graceful shutdown по SIGINT/SIGTERM (Ctrl+C, systemctl stop, docker stop)."""

    def handler(signum, _frame):
        logger.info("Получен сигнал %s, завершаю цикл", signum)
        _shutdown.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):
            # Не основной поток либо сигнал недоступен в этой оболочке.
            pass


def _problems_url(api_url):
    """Ссылка на страницу Problems, выведенная из адреса API."""
    base = api_url.rsplit("/api_jsonrpc.php", 1)[0].rstrip("/")
    return base + "/problems.php"


def collect_state(client, cfg, store):
    """Собирает (active, recovered, holding) — три набора по triggerid.

    active    — проблемы, попавшие под фильтр (value=1).
    recovered — ранее алертили, теперь явно value=0: подтверждённое восстановление.
    holding   — ранее алертили, всё ещё problem, но выпали из фильтра (например,
                упала важность). Это НЕ восстановление, письмо об OK не шлём.
    """
    active = {
        t["triggerid"]: t
        for t in client.get_problem_triggers(
            host_names=cfg.zabbix.host_names,
            min_severity=cfg.zabbix.min_severity,
        )
    }

    recovered = {}
    holding = {}
    pending = [tid for tid in store.pending_ids() if tid not in active]
    if pending:
        for trigger in client.get_triggers_by_ids(pending):
            if str(trigger.get("value")) == "1":
                holding[trigger["triggerid"]] = trigger
            else:
                recovered[trigger["triggerid"]] = trigger

    return active, recovered, holding


def dispatch(actions, mailer, problems_url, dry_run=False):
    """Отправляет письма по списку Action. Возвращает число отправленных."""
    sent = 0
    for action in actions:
        host_name = action.trigger.get("host", "")
        if action.kind == KIND_RECOVERY:
            subject, body = build_recovery_message(
                action.trigger, host_name=host_name, problems_url=problems_url
            )
        else:
            subject, body = build_problem_message(
                action.trigger, host_name=host_name, problems_url=problems_url
            )
            if action.kind == KIND_RENOTIFY:
                subject = "[НАПОМИНАНИЕ] " + subject

        if dry_run:
            logger.info("DRY-RUN, письмо не отправлено:\n%s\n%s", subject, body)
            sent += 1
            continue

        try:
            mailer.send(subject, body)
            sent += 1
        except NotificationError as exc:
            # Ошибка одного письма не должна ронять цикл: остальные дойдут.
            logger.error("Уведомление не доставлено: %s", exc)
    return sent


def notify_channel_state(mailer, cfg, state, subject, body):
    """Служебное письмо о состоянии самого канала связи.

    Идёт через ту же проверку dry_run, что и обычные уведомления: иначе --dry-run
    лез бы в реальный SMTP.
    """
    if state["dry_run"]:
        logger.info("DRY-RUN, служебное письмо не отправлено: %s", subject)
        return
    try:
        mailer.send(subject, body)
    except NotificationError as exc:
        logger.error("Не удалось отправить служебное письмо: %s", exc)


def run_cycle(client, store, mailer, cfg, state):
    """Один цикл опроса. Возвращает True, если API доступен."""
    problems_url = _problems_url(cfg.zabbix.api_url)

    try:
        active, recovered, holding = collect_state(client, cfg, store)
    except (ZabbixAPIError, ZabbixConnectionError) as exc:
        state["api_errors"] += 1
        logger.error(
            "Zabbix API недоступен (подряд %d циклов): %s", state["api_errors"], exc
        )
        # Сообщение отправляем ровно один раз за аварию, а не каждый цикл.
        if state["api_errors"] == cfg.loop.api_alert_after:
            notify_channel_state(
                mailer, cfg, state,
                "[Zabbix][BOT] Нет связи с Zabbix — предположительно отключился "
                "интернет на площадке",
                "zabbix_bot из облака не может опросить Zabbix API %d циклов "
                "подряд (последний интервал — %dс).\n\n"
                "URL: %s\nПоследняя ошибка: %s\n\n"
                "Бот находится вне сети Zabbix, поэтому это значит одно из:\n"
                "  - пропал интернет/связность на площадке;\n"
                "  - недоступен сам Zabbix (сервер, фронтенд, БД);\n"
                "  - отозван или протух API-токен.\n\n"
                "ВАЖНО: это письмо могло НЕ прийти, если почтовый сервер тоже "
                "доступен только изнутри площадки. Отправка идёт через "
                "better-external канал (%s:%d).\n\n"
                "Пока связи нет, уведомления о проблемах Zabbix НЕ отправляются. "
                "Бот продолжит пробовать каждый цикл и сообщит, когда связь "
                "вернётся."
                % (
                    state["api_errors"],
                    cfg.loop.check_interval,
                    cfg.zabbix.api_url,
                    exc,
                    cfg.mail.host,
                    cfg.mail.port,
                ),
            )
        return False

    if state["api_errors"]:
        logger.info("Zabbix API снова доступен после %d сбоев", state["api_errors"])
        notify_channel_state(
            mailer, cfg, state,
            "[Zabbix][BOT] Связь с Zabbix восстановлена",
            "zabbix_bot снова опрашивает Zabbix API после %d неудачных циклов.\n\n"
            "URL: %s\n\n"
            "Уведомления о проблемах отправляются как обычно."
            % (state["api_errors"], cfg.zabbix.api_url),
        )
        state["api_errors"] = 0

    actions = store.process(active, recovered=recovered, holding=holding)
    logger.info(
        "Проблемных триггеров: %d, подтверждено OK: %d, в держании: %d, "
        "уведомлений: %d, в базе: %s",
        len(active),
        len(recovered),
        len(holding),
        len(actions),
        store.stats(),
    )
    dispatch(actions, mailer, problems_url, dry_run=state["dry_run"])
    return True


def build_parser():
    parser = argparse.ArgumentParser(description="Zabbix email-notifier")
    parser.add_argument("--env-file", help="путь к .env (по умолчанию рядом с запуском)")
    parser.add_argument(
        "--once", action="store_true", help="выполнить один цикл и выйти"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="не отправлять письма, только лог"
    )
    parser.add_argument(
        "--test-mail", action="store_true", help="отправить тестовое письмо и выйти"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        cfg = get_config(args.env_file, require_mail=not args.dry_run)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("zabbix_bot запущен (pid %d)", os.getpid())
    logger.info("Конфигурация: %s", describe(cfg))

    client = ZabbixClient(
        cfg.zabbix.api_url, cfg.zabbix.api_token, timeout=cfg.zabbix.http_timeout
    )
    mailer = EmailNotifier(cfg.mail)

    try:
        version = client.version()
        logger.info("Версия Zabbix API: %s", version)
    except (ZabbixAPIError, ZabbixConnectionError) as exc:
        logger.error("Zabbix API не отвечает на apiinfo.version: %s", exc)
        if not args.test_mail:
            return 1

    if args.test_mail:
        try:
            mailer.test()
        except NotificationError as exc:
            logger.error("Тестовое письмо не отправлено: %s", exc)
            return 1
        logger.info("Тестовое письмо отправлено на %s", cfg.mail.recipients)
        return 0

    store = StateStore(
        cfg.loop.state_db_path,
        renotify_after=cfg.loop.renotify_after,
        recovery_confirm_cycles=cfg.loop.recovery_confirm_cycles,
        retention=cfg.loop.state_retention,
    )
    state = {"api_errors": 0, "dry_run": args.dry_run}

    _install_signal_handlers()

    try:
        if args.once:
            run_cycle(client, store, mailer, cfg, state)
            return 0
        while not _shutdown.is_set():
            started = time.monotonic()
            run_cycle(client, store, mailer, cfg, state)
            heartbeat_ping(cfg.loop.heartbeat_url, timeout=cfg.zabbix.http_timeout)
            # Ждём остаток интервала, реагируя на сигнал остановки без задержки.
            remaining = cfg.loop.check_interval - (time.monotonic() - started)
            if remaining > 0:
                _shutdown.wait(remaining)
    except Exception:
        logger.exception("Непредвиденная ошибка в основном цикле")
        return 1
    finally:
        store.close()
        logger.info("zabbix_bot остановлен")

    return 0


if __name__ == "__main__":
    sys.exit(main())