"""Тесты state-машины: дедупликация, recovery, напоминания, хлопушки.

SQLite в temp-файле, без сети и Zabbix.
"""

import os
import tempfile
import time
import unittest

from state import KIND_PROBLEM, KIND_RECOVERY, KIND_RENOTIFY, StateStore


def trigger(triggerid, description="CPU load too high", host="app-01",
            priority="3", lastchange=None, value="1"):
    return {
        "triggerid": str(triggerid),
        "description": description,
        "host": host,
        "priority": priority,
        "lastchange": str(lastchange if lastchange is not None else int(time.time())),
        "value": value,
        "status": "0",
    }


class StateStoreTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self):
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def make_store(self, **kwargs):
        kwargs.setdefault("renotify_after", 0)
        kwargs.setdefault("recovery_confirm_cycles", 2)
        kwargs.setdefault("retention", 86400)
        return StateStore(self.path, **kwargs)

    def test_new_problem_notifies_once(self):
        """Первый цикл даёт письмо, повторный — нет."""
        store = self.make_store()
        self.addCleanup(store.close)

        actions = store.process({"1": trigger(1)})
        self.assertEqual([a.kind for a in actions], [KIND_PROBLEM])

        self.assertEqual(store.process({"1": trigger(1)}), [])

    def test_explicit_recovery_is_immediate(self):
        """Явный value=0 = recovery в том же цикле, без ожидания N пропусков."""
        store = self.make_store(recovery_confirm_cycles=5)
        self.addCleanup(store.close)

        store.process({"1": trigger(1)})
        actions = store.process({}, recovered={"1": trigger(1, value="0")})
        self.assertEqual([a.kind for a in actions], [KIND_RECOVERY])
        self.assertEqual(actions[0].trigger["triggerid"], "1")

    def test_recovery_notified_once(self):
        """Повторных recovery-писем по одному триггеру нет."""
        store = self.make_store()
        self.addCleanup(store.close)

        store.process({"1": trigger(1)})
        store.process({}, recovered={"1": trigger(1, value="0")})
        self.assertEqual(store.process({}, recovered={"1": trigger(1, value="0")}), [])

    def test_disappearance_needs_confirmation_cycles(self):
        """Триггер пропал из ответа — recovery только после N циклов подтверждения."""
        store = self.make_store(recovery_confirm_cycles=2)
        self.addCleanup(store.close)

        store.process({"1": trigger(1)})
        self.assertEqual(store.process({}), [])
        actions = store.process({})
        self.assertEqual([a.kind for a in actions], [KIND_RECOVERY])
        self.assertEqual(actions[0].trigger["description"], "CPU load too high")

    def test_flapping_problem_renotifies_as_new(self):
        """Вернувшийся после recovery триггер — снова PROBLEM."""
        store = self.make_store()
        self.addCleanup(store.close)

        store.process({"1": trigger(1)})
        store.process({}, recovered={"1": trigger(1, value="0")})
        actions = store.process({"1": trigger(1)})
        self.assertEqual([a.kind for a in actions], [KIND_PROBLEM])

    def test_holding_does_not_send_recovery(self):
        """Триггер всё ещё problem, но выпал из фильтра — писем нет, счётчик сброшен."""
        store = self.make_store(recovery_confirm_cycles=1)
        self.addCleanup(store.close)

        store.process({"1": trigger(1)})
        for _ in range(3):
            self.assertEqual(
                store.process({}, holding={"1": trigger(1)}), [],
                "выпавший из фильтра триггер не должен генерировать писем",
            )

        # И только настоящий переход в OK даёт recovery
        actions = store.process({}, recovered={"1": trigger(1, value="0")})
        self.assertEqual([a.kind for a in actions], [KIND_RECOVERY])

    def test_renotify_after_interval(self):
        """Длительная проблема напоминает о себе по истечении интервала."""
        store = self.make_store(renotify_after=2)
        self.addCleanup(store.close)

        self.assertEqual([a.kind for a in store.process({"1": trigger(1)})],
                         [KIND_PROBLEM])
        self.assertEqual(store.process({"1": trigger(1)}), [])

        time.sleep(2.2)
        actions = store.process({"1": trigger(1)})
        self.assertEqual([a.kind for a in actions], [KIND_RENOTIFY])

    def test_multiple_triggers_independent(self):
        """Каждый триггер живёт в своей ветке машины состояний."""
        store = self.make_store(recovery_confirm_cycles=1)
        self.addCleanup(store.close)

        actions = store.process({"1": trigger(1), "2": trigger(2)})
        self.assertEqual({a.kind for a in actions}, {KIND_PROBLEM})
        self.assertEqual({a.triggerid for a in actions}, {"1", "2"})

        actions = store.process({"1": trigger(1)}, recovered={"2": trigger(2, value="0")})
        self.assertEqual([a.kind for a in actions], [KIND_RECOVERY])
        self.assertEqual(actions[0].triggerid, "2")

    def test_state_survives_reopen(self):
        """После рестарта процесса дедуп не сбрасывается — нет спама."""
        first = self.make_store()
        first.process({"1": trigger(1)})
        first.close()

        second = self.make_store()
        self.addCleanup(second.close)
        self.assertEqual(second.process({"1": trigger(1)}), [])

    def test_pending_ids_lists_unresolved(self):
        """pending_ids отдаёт только те, по которым ещё не было recovery-письма."""
        store = self.make_store(recovery_confirm_cycles=1)
        self.addCleanup(store.close)

        store.process({"1": trigger(1), "2": trigger(2)})
        store.process({"1": trigger(1)}, recovered={"2": trigger(2, value="0")})
        self.assertEqual(store.pending_ids(), ["1"])

    def test_stats(self):
        store = self.make_store(recovery_confirm_cycles=1)
        self.addCleanup(store.close)

        store.process({"1": trigger(1), "2": trigger(2)})
        store.process({"1": trigger(1)}, recovered={"2": trigger(2, value="0")})
        self.assertEqual(store.stats(), {"active": 1, "recovered": 1})

    def test_updated_description_is_stored(self):
        """Если макрос в описании изменился, recovery-письмо покажет актуальный текст."""
        store = self.make_store(recovery_confirm_cycles=1)
        self.addCleanup(store.close)

        store.process({"1": trigger(1, description="старое имя")})
        store.process({"1": trigger(1, description="новое имя")})
        actions = store.process({}, recovered={"1": trigger(1, description="новое имя",
                                                            value="0")})
        self.assertEqual(actions[0].trigger["description"], "новое имя")


class ZabbixClientTestCase(unittest.TestCase):
    """Разбор ответов API без сети — через подставную сессию."""

    class FakeSession:
        def __init__(self, body):
            self.headers = {}
            self.body = body
            self.sent = []
            self.auth_seen = []

        def post(self, url, data=None, timeout=None, headers=None):
            import json as _json

            self.sent.append(_json.loads(data))
            self.auth_seen.append((headers or {}).get("Authorization", "kept"))

            outer = self.body

            class Response:
                status_code = 200
                text = ""

                def json(inner_self):
                    if isinstance(outer, ValueError):
                        raise outer
                    return outer

            return Response()

    def make_client(self, body):
        import zabbix_client

        session = self.FakeSession(body)
        return zabbix_client.ZabbixClient(
            "http://zbx/api_jsonrpc.php", "token", timeout=5, session=session
        ), session

    def test_call_returns_result_and_bearer_header(self):
        client, session = self.make_client(
            {"jsonrpc": "2.0", "result": [{"triggerid": "1"}], "id": 1}
        )
        result = client._call("trigger.get", {"output": "extend"})
        self.assertEqual(result, [{"triggerid": "1"}])
        self.assertEqual(session.sent[0]["method"], "trigger.get")
        self.assertTrue(session.headers["Authorization"].startswith("Bearer "))

    def test_version_is_called_without_auth(self):
        """apiinfo.version отвергает запрос с Authorization — проверять без него."""
        client, session = self.make_client(
            {"jsonrpc": "2.0", "result": "7.4.8", "id": 1}
        )
        self.assertEqual(client.version(), "7.4.8")
        self.assertIsNone(session.auth_seen[0])

    def test_problem_triggers_filter_by_host_and_severity(self):
        import zabbix_client

        client, session = self.make_client({"jsonrpc": "2.0", "result": [], "id": 1})
        client.get_problem_triggers(host_names=("app-01",), min_severity=3)
        params = session.sent[0]["params"]
        self.assertEqual(params["filter"]["host"], ["app-01"])
        self.assertEqual(params["filter"]["value"], "1")
        self.assertEqual(params["min_severity"], 3)
        self.assertTrue(params["monitored"])

    def test_hosts_flattened_to_name(self):
        client, _ = self.make_client({
            "jsonrpc": "2.0",
            "result": [{
                "triggerid": "35565",
                "description": "Channel notify AIR is in ERROR state",
                "value": "1",
                "priority": "3",
                "hosts": [{"hostid": "10823", "host": "app-01", "name": "app-01"}],
            }],
            "id": 1,
        })
        triggers = client.get_problem_triggers()
        self.assertEqual(triggers[0]["host"], "app-01")

    def test_json_rpc_error_becomes_exception(self):
        import zabbix_client

        client, _ = self.make_client({
            "jsonrpc": "2.0",
            "error": {
                "code": -32602,
                "message": "Application error.",
                "data": 'No permissions to call "problem.get".',
            },
            "id": 1,
        })
        with self.assertRaises(zabbix_client.ZabbixAPIError) as ctx:
            client.get_problem_triggers()
        self.assertIn("problem.get", str(ctx.exception))

    def test_non_json_response_becomes_connection_error(self):
        """HTML от nginx/PHP-fatal не должен ронять цикл необъяснимым исключением."""
        import zabbix_client

        client, _ = self.make_client(ValueError("no json"))
        with self.assertRaises(zabbix_client.ZabbixConnectionError):
            client.get_problem_triggers()


if __name__ == "__main__":
    unittest.main()
