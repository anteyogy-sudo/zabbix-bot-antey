"""Тесты сборки и рассылки уведомлений, без сети и SMTP."""

import unittest

import main
from notifier import (
    build_problem_message,
    build_recovery_message,
    format_timestamp,
    severity_name,
)
from state import KIND_PROBLEM, KIND_RENOTIFY, Action


def sample_trigger():
    return {
        "triggerid": "35565",
        "description": "Channel notify AIR is in ERROR state",
        "priority": "4",
        "value": "1",
        "lastchange": "1761730000",
        "host": "app-01",
    }


class MessageTestCase(unittest.TestCase):
    def test_problem_message_contains_host_and_severity(self):
        subject, body = build_problem_message(
            sample_trigger(), problems_url="http://zabbix/problems.php"
        )
        self.assertIn("[Zabbix][PROBLEM]", subject)
        self.assertIn("app-01", subject)
        self.assertIn("Хост:        app-01", body)
        self.assertIn("High", body)
        self.assertIn("35565", body)
        self.assertIn("http://zabbix/problems.php", body)
        self.assertIn("Начало:", body)

    def test_recovery_message_marked_as_ok(self):
        subject, body = build_recovery_message(sample_trigger())
        self.assertIn("[Zabbix][RECOVERY]", subject)
        self.assertIn("ВОССТАНОВЛЕНО", body)

    def test_message_without_host_does_not_crash(self):
        trigger = sample_trigger()
        trigger["host"] = ""
        subject, body = build_problem_message(trigger)
        self.assertIn("хост неизвестен", subject)
        self.assertIn("<хост неизвестен>", body)

    def test_severity_name_unknown_value(self):
        self.assertEqual(severity_name("3"), "Average")
        self.assertIn("Неизвестная", severity_name("9"))

    def test_format_timestamp_handles_zero_lastchange(self):
        """У неотработавшего триггера lastchange=0 — не должны печатать 1970 год."""
        self.assertEqual(format_timestamp("0"), "нет данных")
        self.assertEqual(format_timestamp(""), "нет данных")
        self.assertRegex(format_timestamp("1761730000"), r"^\d{4}-\d{2}-\d{2} ")


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


class DispatchTestCase(unittest.TestCase):
    def test_dispatch_routes_host_into_message(self):
        """Регресс: хост не должен уезжать в аргумент problems_url."""
        mailer = FakeMailer()
        actions = [Action(KIND_PROBLEM, "35565", sample_trigger())]

        sent = main.dispatch(actions, mailer, problems_url="http://zbx/problems.php")

        self.assertEqual(sent, 1)
        subject, body = mailer.sent[0]
        self.assertIn("app-01", subject)
        self.assertIn("Хост:        app-01", body)
        self.assertNotIn("Хост:        http", body)

    def test_dispatch_marks_renotify(self):
        mailer = FakeMailer()
        actions = [Action(KIND_RENOTIFY, "35565", sample_trigger())]
        main.dispatch(actions, mailer, problems_url="")
        self.assertIn("[НАПОМИНАНИЕ]", mailer.sent[0][0])
        self.assertIn("[Zabbix][PROBLEM]", mailer.sent[0][0])

    def test_dry_run_sends_nothing(self):
        mailer = FakeMailer()
        actions = [Action(KIND_PROBLEM, "35565", sample_trigger())]
        sent = main.dispatch(actions, mailer, problems_url="", dry_run=True)
        self.assertEqual(sent, 1)
        self.assertEqual(mailer.sent, [])


class ConfigTestCase(unittest.TestCase):
    """Валидация конфига: бот должен падать на старте, а не в цикле."""

    ENV = {
        "ZABBIX_URL": "http://zbx/api_jsonrpc.php",
        "ZABBIX_API_TOKEN": "token",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_SENDER": "bot@example.com",
        "EMAIL_RECIPIENTS": "a@example.com,b@example.com",
    }

    def setUp(self):
        import config

        self.config = config
        self._saved = dict(os_environ_snapshot())

    def tearDown(self):
        import os

        for key in list(os.environ):
            if key.startswith(("ZABBIX_", "SMTP_", "EMAIL_", "CHECK_", "STATE_")):
                del os.environ[key]
        os.environ.update(self._saved)

    def set_env(self, **overrides):
        import os

        for key, value in self.ENV.items():
            os.environ[key] = value
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def load(self, **kwargs):
        """Читает конфиг только из os.environ.

        env_file заведомо несуществующий, иначе load_dotenv подмешает значения
        из рабочего .env, и тесты начнут зависеть от того, что лежит у разработчика.
        """
        kwargs.setdefault("require_mail", True)
        return self.config.get_config(env_file="__no_such_env_file__", **kwargs)

    def test_valid_config_parses_everything(self):
        self.set_env()
        cfg = self.load()
        self.assertEqual(cfg.mail.recipients, ("a@example.com", "b@example.com"))
        self.assertEqual(cfg.mail.port, 587)
        self.assertEqual(cfg.loop.check_interval, 60)
        self.assertTrue(cfg.zabbix.api_token)

    def test_missing_smtp_is_not_fatal_in_dry_run(self):
        self.set_env(SMTP_HOST=None)
        cfg = self.load(require_mail=False)
        self.assertEqual(cfg.mail.host, "")

    def test_missing_smtp_fails_real_run(self):
        self.set_env(SMTP_HOST=None)
        with self.assertRaises(self.config.ConfigError) as ctx:
            self.load()
        self.assertIn("SMTP_HOST", str(ctx.exception))

    def test_all_errors_reported_at_once(self):
        """Не по одному ошибочному запуску, а полным списком."""
        self.set_env(ZABBIX_API_TOKEN=None, SMTP_SENDER=None)
        with self.assertRaises(self.config.ConfigError) as ctx:
            self.load()
        message = str(ctx.exception)
        self.assertIn("ZABBIX_API_TOKEN", message)
        self.assertIn("SMTP_SENDER", message)

    def test_bad_integer_is_reported(self):
        self.set_env(CHECK_INTERVAL="abc")
        with self.assertRaises(self.config.ConfigError) as ctx:
            self.load()
        self.assertIn("CHECK_INTERVAL", str(ctx.exception))

    def test_severity_out_of_range(self):
        self.set_env(ZABBIX_MIN_SEVERITY="9")
        with self.assertRaises(self.config.ConfigError) as ctx:
            self.load()
        self.assertIn("ZABBIX_MIN_SEVERITY", str(ctx.exception))

    def test_host_list_parsing(self):
        self.set_env(ZABBIX_HOSTS="app-01, app-02 ,")
        cfg = self.load()
        self.assertEqual(
            cfg.zabbix.host_names, ("app-01", "app-02")
        )

    def test_describe_hides_secrets(self):
        self.set_env(ZABBIX_API_TOKEN="super-secret-token", SMTP_PASSWORD="pwd123")
        cfg = self.load()
        text = self.config.describe(cfg)
        self.assertNotIn("super-secret-token", text)
        self.assertNotIn("pwd123", text)


def os_environ_snapshot():
    import os

    return {k: v for k, v in os.environ.items()}


if __name__ == "__main__":
    unittest.main()
