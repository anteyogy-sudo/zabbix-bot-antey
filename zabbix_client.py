"""Клиент Zabbix JSON-RPC API поверх requests.

zabbix_utils не используется: в Zabbix 7.x он некорректно передаёт API-токен
(https://github.com/zabbix/python-zabbix-utils/issues/41). Здесь собственный
тонкий клиент: обязательный timeout и явный разбор JSON-RPC ошибок.

Важно про выбранный метод API. Изначально брались на problem.get, но рабочий
токен не имеет на него прав ("No permissions to call problem.get"), тогда как
trigger.get разрешён. Для нашей задачи trigger.get даже удобнее: он отдаёт явный
value (0 = OK, 1 = PROBLEM), то есть восстановление подтверждается положительно,
а не по факту исчезновения из ответа.
"""

import json
import logging

import requests

logger = logging.getLogger(__name__)

TRIGGER_OUTPUT = [
    "triggerid",
    "description",
    "value",
    "priority",
    "status",
    "lastchange",
]


class ZabbixAPIError(Exception):
    """Ошибка на уровне JSON-RPC (неверный токен, нет прав, кривые параметры)."""

    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class ZabbixConnectionError(Exception):
    """Сеть недоступна, HTTP не 2xx, или ответ не является JSON."""


class ZabbixClient:
    def __init__(self, api_url, api_token, timeout=15, session=None):
        self._api_url = api_url
        self._timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Content-Type": "application/json-rpc",
                "Authorization": "Bearer %s" % api_token,
            }
        )
        self._request_id = 0

    def _call(self, method, params=None, need_auth=True):
        """Один JSON-RPC вызов. Возвращает result или бросает исключение.

        need_auth=False нужен для apiinfo.version: Zabbix отказывает этому методу,
        если в запросе присутствует Authorization.
        """
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._request_id,
        }
        headers = None if need_auth else {"Authorization": None}

        try:
            response = self._session.post(
                self._api_url,
                data=json.dumps(payload),
                timeout=self._timeout,
                headers=headers,
            )
        except requests.RequestException as exc:
            raise ZabbixConnectionError(
                "Запрос %s к %s не выполнен: %s" % (method, self._api_url, exc)
            )

        try:
            body = response.json()
        except ValueError:
            snippet = (response.text or "")[:200]
            raise ZabbixConnectionError(
                "Ответ Zabbix не JSON (HTTP %d): %s"
                % (response.status_code, snippet)
            )

        if "error" in body:
            err = body["error"] or {}
            raise ZabbixAPIError(
                "%s: %s %s"
                % (method, err.get("message", "без сообщения"), err.get("data", "")),
                code=err.get("code"),
                data=err.get("data"),
            )

        if "result" not in body:
            raise ZabbixConnectionError(
                "В ответе %s нет ни result, ни error: %s" % (method, body)
            )

        return body["result"]

    def version(self):
        """Версия API без авторизации — дешёвая проверка доступности."""
        return self._call("apiinfo.version", need_auth=False)

    def _get_triggers(self, params):
        """trigger.get с развёрнутыми макросами в описании и именем хоста."""
        params.setdefault("output", TRIGGER_OUTPUT)
        params.setdefault("expandDescription", True)
        params.setdefault("selectHosts", ["host", "name"])
        result = self._call("trigger.get", params)
        return [self._flatten(row) for row in result or ()]

    @staticmethod
    def _flatten(row):
        """hosts: [{host: ...}] -> host: "имя" (хост у триггера обычно один)."""
        hosts = row.get("hosts") or []
        row["host"] = ", ".join(h["host"] for h in hosts)
        return row

    def get_problem_triggers(self, host_names=(), min_severity=0):
        """Активные проблемные триггеры (value=1).

        host_names пустой = все хосты, которые видит пользователь токена.
        min_severity — нижняя граница priority (0..5).
        """
        trigger_filter = {"value": "1", "status": "0"}
        if host_names:
            trigger_filter["host"] = list(host_names)

        params = {"filter": trigger_filter, "monitored": True}
        if min_severity > 0:
            params["min_severity"] = min_severity

        triggers = self._get_triggers(params)
        by_id = {t["triggerid"]: t for t in triggers}
        if len(by_id) != len(triggers):
            logger.warning(
                "В ответе trigger.get повторялись triggerid: %d строк вместо %d",
                len(triggers),
                len(by_id),
            )
        return list(by_id.values())

    def get_triggers_by_ids(self, triggerids):
        """Состояние конкретных триггеров, включая восстановленные (value=0).

        Нужен, чтобы отличить «триггер восстановился» (пришёл с value=0) от
        «триггер исчез» (удалён, хост в maintenance, пропала связь).
        """
        if not triggerids:
            return []
        return self._get_triggers({"triggerids": list(triggerids)})
