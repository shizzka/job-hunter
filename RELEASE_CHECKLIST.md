# Release Checklist (J-001)

Перед каждым релизом / слиянием в main.

## Автоматические проверки

```bash
source venv/bin/activate
set -a && source ~/.job-hunter/job-hunter.env && set +a

# 1. Тесты (smoke + парсеры)
python -m pytest tests/ -v

# 2. Import check
python -c "import agent"

# 3. Синтаксис run.sh
bash -n run.sh
```

## Ручные проверки

- [ ] `./run.sh stats` — выводит статистику без ошибок
- [ ] `./run.sh dry-run` — проходит поиск, не падает
- [ ] Если правился apply/resume код — проверить `./run.sh check` (live)
- [ ] Если правились селекторы hh — проверить `HEADLESS=0 ./run.sh dry-run` визуально
- [ ] Нет лишних `print()` / `breakpoint()` в коде
- [ ] Нет секретов в коммите (ключи, токены, пароли)

## Перед публикацией

- [ ] Обновить версию если нужно
- [ ] `git log --oneline -5` — сообщения коммитов понятны
- [ ] Бэкап state: `cp -r ~/.job-hunter ~/.job-hunter.bak.$(date +%Y%m%d)`
