"""Состояние отправленных уведомлений в SQLite.

Зачем файл, а не память: при рестарте бота (деплой, падение, обновление образа
в облаке) память обнуляется, и следующий цикл шлёт полный список всех активных
проблем заново. SQLite переживает рестарт.

Машина состояний на один цикл опроса. Ключ — triggerid (он стабилен, в отличие
от eventid, который к каждомуProblem-событию свой).

    нет в БД, есть как PROBLEM          -> PROBLEM   (первое письмо)
    есть в БД и всё ещё PROBLEM, долго  -> RENOTIFY  (напоминание)
    есть в БД, пришёл с value=0         -> RECOVERY  (письмо об OK)
    был в БД, исчез из API N циклов     -> RECOVERY  (триггер удалён/снят с контроля)
    вернулся после RECOVERY             -> PROBLEM   (хлопушка)

Исчезновение отдельно от явного value=0, потому что problem-строка может
пропасть из ответа при временной недоступности хоста — это не восстановление,
и подтверждать его надо N циклами подряд.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

KIND_PROBLEM = "problem"
KIND_RENOTIFY = "renotify"
KIND_RECOVERY = "recovery"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (
    triggerid         TEXT PRIMARY KEY,
    description       TEXT NOT NULL DEFAULT '',
    host              TEXT NOT NULL DEFAULT '',
    priority          INTEGER NOT NULL DEFAULT 0,
    lastchange        TEXT NOT NULL DEFAULT '',
    first_seen        INTEGER NOT NULL,
    last_notified     INTEGER NOT NULL DEFAULT 0,
    missed_cycles     INTEGER NOT NULL DEFAULT 0,
    notified_recovery INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Action:
    kind: str
    triggerid: str
    trigger: dict

    @property
    def is_problem(self):
        return self.kind in (KIND_PROBLEM, KIND_RENOTIFY)


class StateStore:
    def __init__(self, path, renotify_after=3600, recovery_confirm_cycles=2,
                 retention=86400):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()
        self._renotify_after = renotify_after
        self._recovery_confirm = max(1, int(recovery_confirm_cycles))
        self._retention = retention

    def close(self):
        self._conn.close()

    def _fetch_all(self):
        rows = self._conn.execute("SELECT * FROM triggers").fetchall()
        return {row["triggerid"]: dict(row) for row in rows}

    def _upsert_active(self, triggerid, trigger, now):
        """Новая проблема или возврат после recovery: сбрасываем счётчики."""
        self._conn.execute(
            """
            INSERT INTO triggers (triggerid, description, host, priority, lastchange,
                                  first_seen, last_notified, missed_cycles,
                                  notified_recovery)
            VALUES (:triggerid, :description, :host, :priority, :lastchange,
                    :now, :now, 0, 0)
            ON CONFLICT(triggerid) DO UPDATE SET
                description       = excluded.description,
                host              = excluded.host,
                priority          = excluded.priority,
                lastchange        = excluded.lastchange,
                last_notified     = :now,
                missed_cycles     = 0,
                notified_recovery = 0
            """,
            {
                "triggerid": triggerid,
                "description": trigger.get("description", ""),
                "host": trigger.get("host", ""),
                "priority": int(trigger.get("priority", 0) or 0),
                "lastchange": str(trigger.get("lastchange", "")),
                "now": now,
            },
        )

    def _touch(self, triggerid, trigger):
        """Проблема жива: обновляем текст и сбрасываем счётчик пропусков."""
        self._conn.execute(
            "UPDATE triggers SET description = ?, host = ?, priority = ?, "
            "lastchange = ?, missed_cycles = 0 WHERE triggerid = ?",
            (trigger.get("description", ""), trigger.get("host", ""),
             int(trigger.get("priority", 0) or 0),
             str(trigger.get("lastchange", "")), triggerid),
        )

    def _mark_recovered(self, triggerid, trigger, now):
        self._conn.execute(
            "UPDATE triggers SET notified_recovery = 1, last_notified = ?, "
            "missed_cycles = 0, description = ?, host = ?, priority = ? "
            "WHERE triggerid = ?",
            (now, trigger.get("description", ""), trigger.get("host", ""),
             int(trigger.get("priority", 0) or 0), triggerid),
        )

    def _bump_missed(self, triggerid):
        self._conn.execute(
            "UPDATE triggers SET missed_cycles = missed_cycles + 1 "
            "WHERE triggerid = ?",
            (triggerid,),
        )

    def process(self, active, recovered=None, holding=None):
        """Сверка ответа API с базой. Возвращает список Action.

        active:    {triggerid: trigger} — триггеры с value=1, попавшие под фильтр.
        recovered: {triggerid: trigger} — те, что раньше были проблемами, а теперь
                   пришли с value=0. Позволяет сообщить о восстановлении сразу,
                   не дожидаясь N циклов отсутствия.
        holding:   {triggerid: trigger} — всё ещё в PROBLEM, но вне фильтра
                   (например, упала важность). Писем не шлём, счётчик пропусков
                   сбрасываем: это не восстановление.
        """
        recovered = recovered or {}
        holding = holding or {}
        now = int(time.time())
        known = self._fetch_all()
        actions = []

        for triggerid, trigger in active.items():
            row = known.get(triggerid)
            if row is None or row["notified_recovery"]:
                self._upsert_active(triggerid, trigger, now)
                actions.append(Action(KIND_PROBLEM, triggerid, trigger))
            else:
                self._touch(triggerid, trigger)
                if (
                    self._renotify_after
                    and now - row["last_notified"] >= self._renotify_after
                ):
                    self._conn.execute(
                        "UPDATE triggers SET last_notified = ? WHERE triggerid = ?",
                        (now, triggerid),
                    )
                    actions.append(Action(KIND_RENOTIFY, triggerid, trigger))

        for triggerid, trigger in recovered.items():
            row = known.get(triggerid)
            if row is None or row["notified_recovery"]:
                continue
            self._mark_recovered(triggerid, trigger, now)
            actions.append(Action(KIND_RECOVERY, triggerid, trigger))

        for triggerid in holding:
            row = known.get(triggerid)
            if row is not None and not row["notified_recovery"]:
                self._conn.execute(
                    "UPDATE triggers SET missed_cycles = 0 WHERE triggerid = ?",
                    (triggerid,),
                )

        for triggerid, row in known.items():
            if (
                triggerid in active
                or triggerid in recovered
                or triggerid in holding
                or row["notified_recovery"]
            ):
                continue
            if row["missed_cycles"] + 1 >= self._recovery_confirm:
                self._mark_recovered(triggerid, row, now)
                actions.append(Action(KIND_RECOVERY, triggerid, dict(row)))
            else:
                self._bump_missed(triggerid)

        self._conn.commit()
        self._cleanup(now)
        return actions

    def pending_ids(self):
        """triggerid активных проблем, по которым ещё не было recovery-письма."""
        rows = self._conn.execute(
            "SELECT triggerid FROM triggers WHERE notified_recovery = 0"
        ).fetchall()
        return [row["triggerid"] for row in rows]

    def _cleanup(self, now):
        """Удаляет старые записи о восстановленных проблемах."""
        cursor = self._conn.execute(
            "DELETE FROM triggers WHERE notified_recovery = 1 AND last_notified < ?",
            (now - self._retention,),
        )
        if cursor.rowcount:
            self._conn.commit()
            logger.debug("Удалено старых записей о recovery: %d", cursor.rowcount)

    def stats(self):
        row = self._conn.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(notified_recovery), 0) AS recovered FROM triggers"
        ).fetchone()
        return {
            "active": row["total"] - row["recovered"],
            "recovered": row["recovered"],
        }
