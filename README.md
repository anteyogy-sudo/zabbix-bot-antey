# zabbix_bot

Опрос Zabbix API из облака и уведомление о проблемах на почту.

Зачем отдельный бот, если у Zabbix есть свои алерты: сервер Zabbix лежит в
локальной сети, а бот — в облаке. Когда в локальной сети пропадает интернет,
сам Zabbix отправить письмо не сможет, а бот увидит, что API недоступен, и
сообщит об этом.

## Как это работает

Раз в `CHECK_INTERVAL` секунд бот вызывает `trigger.get` (фильтр `value=1`,
то есть только проблемы) и сверяет результат с SQLite-базой ранее отправленных
уведомлений. Переход `value 1 -> 0` — восстановление. Повтор одного и того же
события не отправляется, пока оно не сменило состояние.

    main.py           цикл: опрос -> рассылка -> heartbeat
    zabbix_client.py  JSON-RPC поверх requests, Bearer-токен, таймауты
    state.py          SQLite: что уже отправили, дедуп и детект recovery
    notifier.py       SMTP (starttls / ssl / none), сборка текста письма
    heartbeat.py      dead man's switch (healthchecks.io и совместимые)
    config.py         чтение .env и валидация на старте

## Запуск

    python -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
    copy .env.example .env       # и заполнить
    python main.py

CLI:

    python main.py --once        # один цикл и выход (для cron / отладки)
    python main.py --dry-run     # письма в лог, не в SMTP; SMTP можно не настраивать
    python main.py --test-mail   # проверить почту и выйти

## Настройка

Обязательное: `ZABBIX_URL`, `ZABBIX_API_TOKEN`, `SMTP_HOST`, `SMTP_SENDER`,
`EMAIL_RECIPIENTS`. При пустом `ZABBIX_HOSTS` бот мониторит все хосты, которые
видит пользователь токена.

Порог важности — `ZABBIX_MIN_SEVERITY` (0..5, по умолчанию 2 = Warning).
`RENOTIFY_AFTER` — через сколько секунд напомнить о незакрытой проблеме
(0 = не напоминать).

Токен создаётся в Zabbix: Users > API tokens. Права на `trigger.get`
обязательны; на `problem.get` — нет (бот его не использует).

## Тесты

    python -m unittest discover -s tests

## Чего здесь нет

Бот не пишет в Zabbix (не trapper), не ходит по URL напрямую — источником
истины остаётся Zabbix.
