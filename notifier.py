"""Отправка уведомлений на почту через SMTP.

smtplib из стандартной библиотеки, внешних зависимостей нет.
Поддержаны starttls (порт 587), ssl (порт 465) и none (только для отладки
в локальной сети).
"""

import datetime
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from config import SEVERITY_LEVELS

logger = logging.getLogger(__name__)


class NotificationError(Exception):
    """Письмо не отправлено."""


def severity_name(priority):
    try:
        return SEVERITY_LEVELS[int(priority)]
    except (KeyError, TypeError, ValueError):
        return "Неизвестная (%s)" % priority


def format_timestamp(clock):
    """Unix timestamp из Zabbix API (строка) в человекочитаемый вид.

    У триггера, который ни разу не срабатывал, lastchange = 0 — печатать
    «1970-01-01» в письме не стоит.
    """
    raw = "" if clock is None else str(clock).strip()
    if not raw:
        return "нет данных"
    try:
        ts = int(raw)
    except ValueError:
        return raw
    if ts <= 0:
        return "нет данных"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _render(trigger, status_line, problems_url, changed_label):
    """Общий шаблон письма для проблемы и восстановления."""
    host = trigger.get("host") or "<хост неизвестен>"
    description = trigger.get("description") or "Без описания"
    priority = trigger.get("priority", "0")

    lines = [
        "Статус:      %s" % status_line,
        "Хост:        %s" % host,
        "Проблема:    %s" % description,
        "Важность:    %s (%s)" % (priority, severity_name(priority)),
        ("%s:" % changed_label).ljust(13)
        + format_timestamp(trigger.get("lastchange")),
        "ID триггера: %s" % trigger.get("triggerid", "?"),
    ]
    if problems_url:
        lines.extend(["", "Список проблем в Zabbix: %s" % problems_url])
    lines.extend(["", "-- zabbix_bot"])
    return lines


def build_problem_message(trigger, host_name=None, problems_url=""):
    """Собирает (subject, body) для письма о проблеме."""
    host = host_name or trigger.get("host") or "хост неизвестен"
    description = trigger.get("description") or "Без описания"
    subject = "[Zabbix][PROBLEM] %s: %s" % (host, description)
    body = "\n".join(
        _render(trigger, "ПРОБЛЕМА", problems_url, "Начало")
    )
    return subject, body


def build_recovery_message(trigger, host_name=None, problems_url=""):
    """Собирает (subject, body) для письма о восстановлении."""
    host = host_name or trigger.get("host") or "хост неизвестен"
    description = trigger.get("description") or "Без описания"
    subject = "[Zabbix][RECOVERY] %s: %s" % (host, description)
    body = "\n".join(
        _render(trigger, "ВОССТАНОВЛЕНО (OK)", problems_url, "Изменён")
    )
    return subject, body


class EmailNotifier:
    def __init__(self, mail_config):
        self._cfg = mail_config

    def _connect(self):
        """Открывает SMTP-соединение согласно режиму безопасности."""
        if self._cfg.security == "ssl":
            context = ssl.create_default_context()
            return smtplib.SMTP_SSL(
                self._cfg.host,
                self._cfg.port,
                timeout=self._cfg.timeout,
                context=context,
            )

        server = smtplib.SMTP(self._cfg.host, self._cfg.port, timeout=self._cfg.timeout)
        if self._cfg.security == "starttls":
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        return server

    def send(self, subject, body):
        """Отправляет одно письмо всем получателям.

        Бросает NotificationError при любом сбое — вызывающий код решает,
        ронять цикл или нет.
        """
        message = EmailMessage()
        domain = self._cfg.sender.split("@")[-1] or "localhost"
        message["From"] = formataddr(("Zabbix bot", self._cfg.sender))
        message["To"] = ", ".join(self._cfg.recipients)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=domain)
        message["X-Zabbix-Bot"] = "1"
        message.set_content(body)

        server = None
        try:
            server = self._connect()
            if self._cfg.use_auth:
                server.login(self._cfg.username, self._cfg.password)
            server.send_message(message)
            logger.info("Письмо отправлено: %s", subject)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotificationError(
                "Не удалось отправить %r через %s:%d: %s"
                % (subject, self._cfg.host, self._cfg.port, exc)
            )
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass

    def test(self):
        """Проверка конфигурации почты: шлёт тестовое письмо всем получателям."""
        self.send(
            "[Zabbix][TEST] Проверка работы zabbix_bot",
            "Тестовое письмо от zabbix_bot. Конфигурация SMTP корректна:\r\n"
            "сервер %s:%d, режим %s, отправитель %s."
            % (self._cfg.host, self._cfg.port, self._cfg.security, self._cfg.sender),
        )
