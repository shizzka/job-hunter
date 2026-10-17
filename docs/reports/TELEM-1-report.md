# TELEM-1: офлайн-расследование и приватный журнал

Результат: выполнены A+B. Правило C не реализовано и не включено: второй
неодобренный POST всё ещё может быть действием. Ограничение автопоиска этим
изменением не снято. Живых обращений к HH не было.

## Доказательства и их предел

Проверены все девять указанных trace.jsonl. Во всех `APPLY_EXCEPTION` содержит
owned POST 200 и второй unowned POST без наблюдаемого статуса, с
`path_category=other`, `body_state=malformed`, `application_reason=malformed_body`.
В каждом прогоне 16–19 событий context (32–38 с учётом page), в том числе
12–14 пустых HH POST, один malformed HH POST, один readable HH POST и
2–3 readable third-party POST. Это одна и та же структурная картина.

Найдены десять сохранённых JS; все в каталоге от **11 октября**, а события — от
16 октября. Каталог исходников (далее `JS/`):

`~/.job-hunter/profiles/qa/traces/2026-10-11/hh_138187936_182604/existing-response-diagnostic/`

JS подтверждает следующие отправители. Все относительные URL используют origin
текущего документа; для HH-страницы это HH-host, но точный hostname живого запроса
старый трейс не сохраняет.

| Источник | Путь, метод | Тело и параметры | Что доказано |
| --- | --- | --- | --- |
| `JS/resource-0.js:103`, col 265013 | `/anatskytics`, POST beacon, fallback POST | Blob: `application/x-www-form-urlencoded` либо `application/json`; body — сериализация переданного объекта (по умолчанию пустой); query — analyticsParams, параметры события, originalRequestId | Аналитика событий/показов/кнопок; возможный источник пустых POST. Полный набор query-полей зависит от вызывающего кода. |
| `JS/resource-0.js:103`, col 266088 | `/shards/tick`, POST beacon, fallback POST | FormData: `data` (JSON), `_xsrf`; fallback — urlencoded body и query с analyticsParams, параметрами события, originalRequestId | Аналитика событий. Нет связи с точным живым запросом. |
| `JS/resource-0.js:26`, col 924031 и 925293 | `/shards/stat`, POST beacon/fetch | multipart FormData: повторяемое `metrics` (JSON), `_xsrf`; статический URL без query | Performance/navigation timings. Нет связи с точным живым запросом. |
| `JS/resource-3.js:1`, col 950647 и 951011 | `/shards/anything_of_the_day_analytics`, `/shards/banner_real_view_analytics`, POST beacon/fallback | Blob `application/x-www-form-urlencoded`: `viewUrl`, `_xsrf`, дополнительные параметры баннера; для clickUrl у первого endpoint есть отдельный GET | Показ баннеров, а не доказанная сигнатура наблюдаемых пустых POST. |
| `JS/resource-7.js:1`, col 34163 | `/applicant/vacancy_response/popup`, POST | FormData с vacancy_id, resume_hash, letter, country_ids и другими полями одобренного payload | Реальный отклик, остаётся под одноразовым approval. |

Ключевые фрагменты:

```js
// resource-0.js:103 — /anatskytics
const e={type:n?"application/json":"application/x-www-form-urlencoded"},
      r=new Blob([n?JSON.stringify(t):o.j.stringify(t)],e);
if(navigator.sendBeacon(`/anatskytics?${o.j.stringify(s)}`,r))
    return Promise.resolve();
// s = {...analyticsParams, ...eventParams, originalRequestId: ...}
// module 707927, resource-0.js:103 col 272850: getter analyticsParams

// resource-0.js:103 — /shards/tick
t.append("data",JSON.stringify(s)); t.append("_xsrf",e);
navigator.sendBeacon("/shards/tick",t)

// resource-0.js:26 — performance metrics
i.append("metrics",JSON.stringify({...r,...e})); i.append("_xsrf",e);
navigator.sendBeacon(this._url,i)
// constructor: url:"/shards/stat"
```

В `resource-7.js:1`, col 35869, обработчик успешного popup POST вызывает
`(0,L.BH)("vacancy_response_action")` и обновляет UI/уведомления. Это подтверждает
наличие post-submit аналитики, но не устанавливает личность второго POST.
В JS также есть реальные сторонние действия после response UI: например,
`resource-8.js:1`, col 40393 — `/shards/chat/send_message`, а col 59095 —
`/shards/applicant/mark_additional_data_screens_shown` с vacancyId и resumeHash.
В `resource-3.js:1`, col 922548, изменение видимости резюме на
`/shards/resume/edit/visibility` тоже несёт hhtmFromLabel. Трекинговые имена сами
по себе не доказывают пассивность запроса.

## Второй malformed POST

**Точный отправитель и точный путь не установлены.** Доступные события уже
маскируют их. Из `path_category=other` следует, что классификатор не распознал
его как RESPONSE_PATH или /popup; это не исключает действие на другом пути.
`status=null` здесь не доказывает отсутствие сетевого ответа: response observer
в recovery обновляет статус только на response-path.

`hh/recovery.py:_response_evidence` сначала ставит `malformed`, затем пробует
декодировать JSON или multipart. При исключении состояние остаётся malformed.
При text/plain/urlencoded тело, начинающееся с `{` или `[`, тоже пробуется как
единый JSON. Поэтому malformed — результат нашего разбора, а не семантика HH.

В `JS/resource-0.js:103`, col 391101, bundled Sentry serializer собирает envelope:

```js
let r=JSON.stringify(t);
// Для каждой записи:
o(`\n${JSON.stringify(t)}\n`);
// Затем добавляет payload записи.
```

Транспорт (`resource-0.js:103`, col 430672) использует fetch с method POST;
endpoint вычисляется из DSN либо tunnel (`function Yr`, col 410391).
Такой текст способен воспроизвести `malformed_body` при text/plain, потому что
несколько JSON-записей не являются единым JSON-документом. Это **гипотеза**, не
идентификация живого запроса: его content-type, адрес и длина ранее не записаны.
Повреждённое тело действия может иметь ту же публичную классификацию.

Кроме того, `_response_evidence` пишет в field_sources только пересечение с
PAYLOAD_FIELDS. Видимые hhtmFromLabel/hhtmSourceLabel/vacancyId не означают,
что иных query-ключей не было. Полное совпадение сигнатуры из старых трасс
проверить невозможно.

## Решение по C и продолжению

Решение по `HH_PASSIVE_TELEMETRY_ENABLED`: **0 / правило отсутствует**.
По условию A+B новый флаг и matcher не добавлены; установка env-переменной в 1
тоже не разрешает запросы. Добавлять действующий переключатель без доказанного
набора сигнатур было бы преждевременно. Утренняя запись адресов позволит решить,
допустимо ли отдельно реализовать C.

Решение о продолжении в этом коммите **не меняется**:

- `hh/recovery.py:270`, `_unknown_requires_stop`: third-party уже пассивен;
  possible application_request всегда требует стоп. Старое исключение для
  прочего HH-трафика требует единственного owned POST со статусом 200; второй
  malformed request разрушает это условие, а предыдущие unknown остаются sticky.
- `hh/verification.py:104`, `_owned_post`: необходимо ровно одно owned POST 200.
- `hh/verification.py:323`, `_session_intact`: unknown, guard, session/auth или
  binding проблемы запрещают продолжение.
- `hh/verification.py:368` / `:451`: prepare_uncertain_continuation и
  can_continue_uncertain требуют независимого доказательства целости сессии.
- `hh/apply.py:879`, `:901`, `:935`, `:954`: непройденная проверка продолжения
  оставляет hh_hard_stop; `agent.py:953` перепроверяет receipt перед продолжением.

Durable uncertain не превращается в completed, повтор не разрешается; B3-11
остаётся отдельным фактом доставки. Антибот, captcha, auth и HHUnexpectedUI
не менялись. Исключение для third-party не расширялось.

## Новый приватный журнал

Путь: **`<captured HH_STATE_DIR>/hh_unknown_requests.jsonl`**.
Для обычного профиля это `~/.job-hunter/profiles/<profile>/state/hh_unknown_requests.jsonl`.

Пишется всегда при неизвестном запросе в окне native/manual HH attempt, также
для сохранённого владельца после освобождения _external_attempt. Не зависит от
trace, env-флага или решения requires_stop. События page и context записываются
раздельно; worker/unobserved frame сохраняет context scope.

Поля: observed_at (UTC), scope, observation, reason, host (без userinfo),
path (без query и fragment, без нормализации), method, content_type (только MIME,
без charset/boundary и иных параметров), полный список имён query_fields,
body_length (байты), since_owned_post_ms (от начала наблюдаемого owned POST).
До owned POST offset=null; недоступные свойства=null. Cookies, authorization,
query-значения, body и тексты исключений не сериализуются. Точный путь может
содержать route identifier, поэтому остаётся только в приватном журнале.

Destination захватывается при установке context monitor, без глобального
fallback. Поздний запрос старого context берёт его собственного сохранённого
владельца, даже если у сессии уже есть active attempt другого context.
Общий private append helper обеспечивает 0600, flock/fsync,
запрет symlink/nonregular target и восстановление прав существующего файла.
Ошибка записи выводит только класс исключения, не меняет stop/claim/delivery;
сбой обычной trace-записи не мешает приватному журналу.

## Проверки

Smoke boundary: Level 0, исключительно синтетические события, mocked server
delivery и существующие browser E2E с offline route fulfillment. Внешних
действий и обращений к HH не было. Полный сьют не запускался.

Команды и результаты записаны в `/tmp/telem-1-FuYn7P1W/`:

```bash
venv/bin/python -m pytest -q tests/test_hh_passive_frame_observation.py tests/test_hh_apply_uncertainty.py tests/test_hh_uncertain_continuation.py tests/test_e2e_hh_run_continuity.py tests/test_e2e_hh_apply_happy_path.py tests/test_hh_confirmed_success_precedence.py tests/test_hh_server_delivery.py tests/test_hh_delivery_outcome.py > /tmp/telem-1-FuYn7P1W/gates.log 2>&1
venv/bin/python -m pytest -q tests/test_hh_request_journal.py tests/test_hh_unknown_diagnostics.py tests/test_hh_unowned_request_scope.py tests/test_hh_recovery_attempt_window.py tests/test_hh_post_submit_traffic.py tests/test_hh_response_path.py > /tmp/telem-1-FuYn7P1W/focused-repeat.log 2>&1
venv/bin/python -m pytest -q tests/test_hh_request_journal.py > /tmp/telem-1-FuYn7P1W/journal-final-22.log 2>&1
```

Обязательные гейты: **176 passed за 510.31 s, exit 0**. Целевые проверки:
**610 passed за 217.38 s, exit 0**. Финальные проверки журнала, включая поздний
foreign context: **22 passed за 21.17 s, exit 0**.
Production diff: `/tmp/telem-1-FuYn7P1W/production.diff`.

Первый целевой запуск (`focused.log`) дал 609 passed, но завершился exit 1:
защитная проверка изоляции заметила изменение
`~/.job-hunter/profiles/qa/knowledge/kb_eval_questions.txt` во время запуска.
В изменённом коде и выбранных тестах нет обращения к этому файлу; файл не
восстанавливался и не изменялся этой задачей. Повтор проведён без изменения
защитного механизма и завершился exit 0. Исходный лог сохранён.

Проверяемый regression: owned POST 200, сервер подтверждает доставку, затем
второй возможный action и 16 пустых POST → unknown, uncertain, запрет повтора
и запрет продолжения сохраняются при env=0 и env=1. Позитивный сценарий с
включённым C не заявляется: такого правила по результатам A не существует.

TELEM_1_DONE
