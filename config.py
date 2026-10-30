"""Конфигурация бота: загрузка из .env и валидация на старте.

Все обязательные параметры проверяются в get_config(). Бот падает сразу с
понятным списком проблем, а не с None в середине цикла.
"""

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

SEVERITY_LEVELS = {
    0: "Not classified",
    1: "Information",
    2: "Warning",
    3: "Average",
    4: "High",
    5: "Disaster",
}

SMTP_SECURITY_MODES = ("starttls", "ssl", "none")


class ConfigError(Exception):
    """Конфигурация некорректна или неполна."""


@dataclass(frozen=True)
class ZabbixConfig:
    api_url: str
    api_token: str
    http_timeout: int
    min_severity: int
    host_names: tuple


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str
    recipients: tuple
    timeout: int

    @property
    def use_auth(self) -> bool:
        return bool(self.username and self.password)


@dataclass(frozen=True)
class LoopConfig:
    check_interval: int
    state_db_path: str
    renotify_after: int
    recovery_confirm_cycles: int
    state_retention: int
    heartbeat_url: str
    api_alert_after: int


@dataclass(frozen=True)
class Config:
    zabbix: ZabbixConfig
    mail: MailConfig
    loop: LoopConfig
    log_level: str = "INFO"


def _env(name, default=""):
    return (os.getenv(name) or default).strip()


def _env_int(name, default, minimum=0):
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("%s должен быть целым числом, получено %r" % (name, raw))
    if value < minimum:
        raise ConfigError("%s должен быть >= %d, получено %d" % (name, minimum, value))
    return value


def _env_list(name):
    """Разбирает значение вида "a, b,c" в кортеж непустых элементов."""
    raw = _env(name)
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _build_zabbix(errors):
    api_url = _env("ZABBIX_URL")
    api_token = _env("ZABBIX_API_TOKEN")

    if not api_url:
        errors.append("ZABBIX_URL не задан (ожидается .../api_jsonrpc.php)")
    if not api_token:
        errors.append("ZABBIX_API_TOKEN не задан (Users > API tokens в Zabbix UI)")

    min_severity = _env_int("ZABBIX_MIN_SEVERITY", 2, minimum=0)
    if min_severity > 5:
        errors.append(
            "ZABBIX_MIN_SEVERITY должен быть 0..5, получено %d" % min_severity
        )

    return ZabbixConfig(
        api_url=api_url,
        api_token=api_token,
        http_timeout=_env_int("HTTP_TIMEOUT", 15, minimum=1),
        min_severity=min_severity,
        host_names=_env_list("ZABBIX_HOSTS"),
    )


def _build_mail(errors):
    host = _env("SMTP_HOST")
    sender = _env("SMTP_SENDER")
    recipients = _env_list("EMAIL_RECIPIENTS")
    security = _env("SMTP_SECURITY", "starttls").lower()

    if not host:
        errors.append("SMTP_HOST не задан")
    if not sender:
        errors.append("SMTP_SENDER не задан (адрес в поле From)")
    if not recipients:
        errors.append("EMAIL_RECIPIENTS не задан (хотя бы один адрес через запятую)")
    if security not in SMTP_SECURITY_MODES:
        errors.append(
            "SMTP_SECURITY должен быть одним из %s, получено %r"
            % (list(SMTP_SECURITY_MODES), security)
        )

    return MailConfig(
        host=host,
        port=_env_int("SMTP_PORT", 587, minimum=1),
        security=security,
        username=_env("SMTP_USERNAME"),
        password=_env("SMTP_PASSWORD"),
        sender=sender,
        recipients=recipients,
        timeout=_env_int("SMTP_TIMEOUT", 15, minimum=1),
    )


def _build_loop(errors):
    recovery_confirm = _env_int("RECOVERY_CONFIRM_CYCLES", 2, minimum=1)
    if recovery_confirm > 10:
        errors.append(
            "RECOVERY_CONFIRM_CYCLES слишком большой (максимум 10): "
            "recovery будет определяться с большим запозданием"
        )

    return LoopConfig(
        check_interval=_env_int("CHECK_INTERVAL", 60, minimum=1),
        state_db_path=_env("STATE_DB_PATH", "state.db"),
        renotify_after=_env_int("RENOTIFY_AFTER", 3600, minimum=0),
        recovery_confirm_cycles=recovery_confirm,
        state_retention=_env_int("STATE_RETENTION", 86400, minimum=60),
        heartbeat_url=_env("HEARTBEAT_URL"),
        api_alert_after=_env_int("ZABBIX_ALERT_AFTER_CYCLES", 3, minimum=1),
    )


def get_config(env_file=None, require_mail=True):
    """Читает .env, собирает и валидирует конфигурацию.

    env_file можно передать явно для тестов; по умолчанию ищется .env рядом
    с точкой запуска. require_mail=False отключает проверку SMTP — нужно для
    --dry-run, когда письма не отправляются.
    """
    load_dotenv(dotenv_path=env_file)

    errors = []
    mail_errors = []
    config = Config(
        zabbix=_build_zabbix(errors),
        mail=_build_mail(mail_errors),
        loop=_build_loop(errors),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
    )
    if require_mail:
        errors.extend(mail_errors)

    if errors:
        message = "\n".join("  - %s" % e for e in errors)
        raise ConfigError("Конфигурация некорректна:\n%s" % message)

    return config


def describe(config):
    """Строка для лога при старте: что мониторим и куда шлём. Без секретов."""
    hosts = ", ".join(config.zabbix.host_names) or "<все доступные хосты>"
    return (
        "Zabbix API: %s; "
        "хосты: %s; "
        "мин. важность: %d (%s); "
        "почта: %s через %s:%d (%s); "
        "интервал: %dс; heartbeat: %s"
        % (
            config.zabbix.api_url,
            hosts,
            config.zabbix.min_severity,
            SEVERITY_LEVELS[config.zabbix.min_severity],
            ", ".join(config.mail.recipients) or "<не задана>",
            config.mail.host or "<не задан>",
            config.mail.port,
            config.mail.security,
            config.loop.check_interval,
            "задан" if config.loop.heartbeat_url else "не задан",
        )
    )