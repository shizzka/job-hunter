# split-22c runtime validation

Finding: **blocker 1, validation gap** from `critic-split-22c.md`.

The critic identified no confirmed implementation defect or should-fix finding. The corrective change is retained execution evidence; application code, tests, fixtures, assertions, and safety checks are unchanged.

Runtime code reviewed: `106751dad0ae0c234c5dae9997d7debfaf631742`. Characterization reference: `ac63d85263d8fa7f26130568d3237ddee78ccd77`.

## Execution controls

The reference ran from a disposable detached worktree. Each run received an explicit environment allowlist with separate temporary HOME, state, logs, temporary files, and cache directories. No inherited credentials or profile configuration were supplied.

The existing test harness rebases state paths, forbids network sockets in ordinary tests, and checks protected-file fingerprints at session end. Subprocess tests retain their existing synthetic fixtures. These are the project regression-test controls; this record does not claim an OS-sandboxed security audit.

Preinstalled Playwright browser binaries were supplied through `PLAYWRIGHT_BROWSERS_PATH`; no dependencies were downloaded. The first HEAD attempt could not locate the browser after cache isolation. The runner environment was corrected and the complete selection rerun; the failed attempt log is retained separately.

## Results

| Revision and check | Result |
|---|---|
| `ac63d85`: all three frozen characterization files | 1906 passed in 296.83s (0:04:56); exit 0 |
| `106751d`: `python -c "import telegram_bot"` | exit 0; no output |
| `106751d`: bot/isolation selection, including all three frozen files | 3154 passed in 329.39s (0:05:29); exit 0 |

Pytest used `-q -x -n 4 -p no:cacheprovider`. The baseline selection was:

```text
tests/test_telegram_handler_characterization.py
tests/test_telegram_dispatch_characterization.py
tests/test_telegram_callback_characterization.py
```

The HEAD selection was every tracked `tests/test_telegram*.py` file plus:

```text
tests/test_bot_profile_isolation.py
tests/test_instance_configuration.py
tests/test_bot_state_transactions.py
tests/test_state_isolation.py
tests/test_chat_owner_picker.py
tests/test_hh_ui_isolation.py
```

Total HEAD selection: 38 files. All three frozen files were explicitly included.

## Frozen evidence identity

Each blob below is identical at `ac63d85` and `106751d`:

| File | Git blob |
|---|---|
| `tests/test_telegram_handler_characterization.py` | `d85f61e5c5202ba38c3ffb3c8477cf7000897657` |
| `tests/test_telegram_dispatch_characterization.py` | `cdbf75cf398717776b731e78008d866c05a95bf4` |
| `tests/test_telegram_callback_characterization.py` | `b14f04c338813037aa646a7c7be16e4a34365339` |
| `tests/fixtures/telegram_handlers.json` | `489253ed8c0188e7dc25e1d9b66a91cc02be6ea9` |
| `tests/fixtures/telegram_dispatch.json` | `ec3bf3e2f3b1cb4b98a5b921095aadc8cad10271` |
| `tests/fixtures/telegram_callbacks.json` | `14b877d1fd862ae9b46e02a282263ecad9582f4b` |

## Retained execution output

| Check | Raw log | SHA-256 |
|---|---|---|
| baseline | `security-audit/suites/split-22c-critic-validation-JCC3LU/baseline/pytest.log` | `dce427a144921c80c2fbf8bddc81cf72b637235182acea9b5305d5ca20a59a0d` |
| head initial environment failure | `security-audit/suites/split-22c-critic-validation-JCC3LU/head/pytest-attempt1.log` | `9778213707cb5b09b29871e13a6ce7cac20abfbea3f900c718fedd2ce46a5861` |
| head | `security-audit/suites/split-22c-critic-validation-JCC3LU/head/pytest.log` | `15834f16a70450923612b6f493ba25816503b25e5af916a57b57baa6f23ac78b` |

The full-suite helper is the final post-commit gate for `refactor/gate-2` in `/mnt/work/jh-stab`. It retains the full suite log; the task completion report records its summary and candidate commit.
