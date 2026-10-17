# TELEM-2: точные подписи пассивной телеметрии

`HH_PASSIVE_TELEMETRY_ENABLED` по умолчанию равен **1**. Значение **0**
восстанавливает прежние правила наблюдения, включая эвристический второй POST
для Sentry. Флаг читается при наблюдении запроса.

## Доказательства и границы

Локально прочитан приватный журнал профиля qa-dev для отклика 17.10.2026
10:49, trace `hh_138568573_104955`: 32 записи page/context для 16 запросов.
Агрегаты полностью совпали с заданием. В fixture
`tests/fixtures/hh_telem_2_signatures.json` сохранены только host, path, method,
MIME, имена query-полей и длины тел для 16 context-записей в исходном порядке.
Значения query и содержимое тел туда не попадают; тесты создают синтетические тела.

Все четыре подписи включены. `/anatskytics` дополнительно подтверждён JS из
TELEM-1; точный Sentry endpoint снимает прежнюю неопределённость отправителя
malformed POST. Другие перечисленные в TELEM-1 `/shards/*` не включены.

| Host | Точный путь | MIME | Максимум тела, байт | В журнале |
| --- | --- | --- | ---: | --- |
| `hh.ru` или один региональный label `.hh.ru` | `/anatskytics` | `application/x-www-form-urlencoded` | 256 | `spb.hh.ru`, 12 × 0 |
| `sentry.hh.ru` | `/api/[0-9]+/envelope/` (полное совпадение) | `text/plain` | 65536 | `/api/607/envelope/`, 1 × 453 |
| `cpa.hh.ru` | `/api/tracker/hit` | `text/plain` | 256 | 1 × 2 |
| `eye.targetads.io` | `/web/collect` | `text/plain` | 4096 | 146 и 202 |

Везде только POST; query-поля должны быть подмножеством полного множества из
задания, зафиксированного в `hh/passive_telemetry.py`. Проверяется длина wire bytes,
включая верхнюю границу. MIME сравнивается без charset и других параметров, как
в журнале TELEM-1. Закодированные тела, недоступные свойства, иные хосты/пути,
лишние query-ключи, иной MIME или превышение лимита не классифицируются как passive.
Разрешены только HTTPS origins без credentials, нестандартного порта или fragment;
пути не нормализуются. Региональный хост не допускает вложенных subdomains.

## Почему сессия теперь продолжается

- `hh/recovery.py:request_seen` сначала проверяет полную подпись. Совпавший
  запрос имеет классификацию `passive_telemetry` и не входит в application POSTs,
  sequence fence, unknown, `HH_UNKNOWN_ACTION` или журнал неизвестных запросов.
  `_application_request` тоже проверяет подпись до `malformed_body`.
- В режиме 1 любой оставшийся изменяющий запрос в окне активной/сохранённой
  попытки требует стоп. Это необходимо для отрицательных примеров: прежнее
  общее послабление third-party и unrelated HH-after-owned-200 иначе пропускало бы
  лишний query-ключ anatskytics или Sentry на постороннем host. В режиме 0 старые
  послабления сохранены и отдельно проверяются тестами.
- `hh/verification.py:89`, `_owned_post`, по-прежнему требует ровно один owned POST 200,
  точные approval/payload, vacancy, resume, owner и durable state. Теперь Sentry
  не создаёт второй элемент, и `reconcile_post_submit` снова может выполнить
  обычную независимую сверку страницы вакансии с сервером.
- `hh/verification.py:323`, `_session_intact`, сохраняет все проверки неизвестных событий,
  guard, auth, cookie binding, owner и sequence. Пассивные события не меняют эти
  свидетельства; уже установленный sticky unknown они не снимают.
- `hh/apply.py:848–954` сначала использует `confirmed_result` после сверки (включая
  обработку исключения после отправки). Если исход ещё uncertain, продолжение
  возможно только через `prepare_uncertain_continuation` и последующий
  `can_continue_uncertain`; иначе остаётся `hh_hard_stop=True`. Подтверждение
  доставки B3-11 само по себе этих условий не обходит.
- `agent.py:953`, `_HHApplyReceipt.can_continue`, по-прежнему требует отсутствие hard stop
  и anti-bot и действующий typed receipt целостности сессии для uncertain.
  Подтверждённый обычной сверкой успех проходит обычный путь поиска; uncertain
  продолжает только при отдельной проверке сессии. `agent.py:1211` повторно
  проверяет promoted receipts после await и при потере целостности останавливает HH.

В живой форме owned 200 + 16 точных телеметрических запросов дают
`posts=[owned]`, без unknown и hard stop при успешной сверке. Серверная доставка
остаётся отдельным фактом. Если серверная страница не подтверждает итог,
durable uncertain сохраняется; при доказанной целости сессии можно перейти к
следующей вакансии без повтора предыдущего отклика.

## Сохранённые инварианты

Повторный POST на RESPONSE_PATH/popup всегда остаётся действием и вызывает стоп.
Пассивный matcher не допускает эти пути. Durable claim, запрет повторов,
сохранённый uncertain и публикация доставки не менялись. Captcha, антибот,
неожиданный UI, потеря auth/binding и неизвестные события сохраняют стоп.
Неизвестные запросы по-прежнему пишутся приватным журналом TELEM-1; совпавшие
пассивные запросы не пишутся. Ошибка журнала не меняет action boundary.

## Проверки

Только offline: синтетические события и Chromium с локальным route fulfillment,
DNS запретом для внешних browser hosts и подменёнными read-only transport hooks.
К HH не обращались, полный сьют не запускался.

Наборы проверок (логи `/tmp/telem-2-*.log`):

```bash
venv/bin/python -m pytest -q tests/test_hh_passive_telemetry.py tests/test_hh_recovery_attempt_window.py
venv/bin/python -m pytest -q tests/test_hh_passive_telemetry_e2e.py
venv/bin/python -m pytest -q tests/test_hh_request_journal.py tests/test_hh_unknown_diagnostics.py tests/test_hh_unowned_request_scope.py tests/test_hh_recovery_attempt_window.py tests/test_hh_post_submit_traffic.py tests/test_hh_response_path.py
venv/bin/python -m pytest -q tests/test_hh_passive_frame_observation.py tests/test_hh_apply_uncertainty.py tests/test_hh_uncertain_continuation.py tests/test_e2e_hh_run_continuity.py tests/test_e2e_hh_apply_happy_path.py tests/test_hh_confirmed_success_precedence.py tests/test_hh_server_delivery.py tests/test_hh_delivery_outcome.py tests/test_hh_post_submit_verification.py
```

Регрессии проверяют восстановленные 16 запросов, owned-only POSTs, confirmed
доставку, продолжение поиска после подтверждения и после проверенного uncertain,
лишний query-ключ, неверные MIME/method/host/path, превышение лимитов,
повторный popup, выключенный флаг и сохранение sticky unknown/durable uncertain.
Старые проверки общего послабления сохранены явно в режиме 0; default-проверки
требуют стоп для недоказанных HH и сторонних мутаций.

Итог: **905 уникальных целевых сценариев покрыты успешными проверками**.

| Набор | Результат | Лог |
| --- | --- | --- |
| Новый classifier + окно попытки | 91 passed, 32.13 s (61 новый + 30 повторных) | `/tmp/telem-2-classifier.log` |
| Журнал, diagnostics, scope, attempt window, traffic, response paths | 635 passed, 176.47 s | `/tmp/telem-2-focused-final.log` |
| Новый Chromium replay, включая stale DOM после отправки | 8 passed, 121.23 s | `/tmp/telem-2-e2e-verified.log` |
| Гейты TELEM-1 + post-submit verification | 180 остальных кейсов прошли в основном запуске; оба файла с исправленными ожиданиями полностью перепроверены ниже | `/tmp/telem-2-gates.log` |
| Happy path и post-submit traffic | 14 passed, 249.05 s | `/tmp/telem-2-happy-final.log` |
| Приоритет подтверждения и поздний unproven PUT, оба режима флага | 7 passed, 27.88 s | `/tmp/telem-2-precedence-final.log` |

Основной запуск гейтов дал 198 passed / 2 failed за 560.15 s. Оба падения
касались тестов: новый anatskytics fetch был намеренно aborted offline transport
и требовал `.catch`, чтобы не прерывать mocked read hook; старое ожидание
разрешало поздний PUT после success. После исправления оба файла полностью
перепроверены: 14 + 7 passed. Добавлен отдельный legacy-case, поэтому финальный
объём этого набора — 201 уникальный сценарий. Непроверенных падений не осталось.
Первый focused-запуск также обнаружил старый assert о page.unknown после позднего
стороннего POST; исправленный набор целиком дал 635 passed. При подготовке нового
E2E уточнено ожидание shutdown: recovery и агент могут вызвать его дважды,
отрицательные кейсы требуют хотя бы один hard stop.

Повторные команды после исправлений:

```bash
venv/bin/python -m pytest -q tests/test_e2e_hh_apply_happy_path.py
venv/bin/python -m pytest -q tests/test_hh_confirmed_success_precedence.py
venv/bin/python -m pytest -q -x tests/test_hh_passive_telemetry_e2e.py
```

`git diff --check` и candidate report прошли. Живой браузерный прогон не
выполнялся по условию задачи; вывод о текущем live поведении ограничен точными
подписями предоставленного журнала и их offline воспроизведением.
