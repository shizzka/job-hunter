"""Bounded, explicit DeepSeek diagnostics; never uses provider fallback."""
import asyncio
import time
from urllib.parse import urlparse

from llm_client import _build_provider_specs
import proxy_utils

TEST_MODEL = "deepseek-flash"


def deepseek_provider():
    return next((p for p in _build_provider_specs() if p.name == "deepseek"), None)


def overview():
    provider = deepseek_provider()
    if not provider:
        return "DeepSeek не настроен. Добавьте ключ и адрес в llm-providers.env на сервере."
    model = provider.model_for("gpt-oss:20b")
    return (f"🧠 Платные LLM · общий аккаунт сервера\nDeepSeek: настроен\n"
            f"Сервер: {urlparse(provider.base_url).hostname}\nНастройка рабочего поиска: {model}\nМодель теста: {TEST_MODEL}\n\n"
            "Тест: один короткий платный запрос, максимум 256 выходных токенов, не чаще раза в минуту. "
            "Резюме и вакансии не передаются. Кнопки не меняют маршрутизацию рабочего поиска.")


async def diagnostic(*, test=False):
    provider = deepseek_provider()
    if not provider:
        return overview()
    try:
        return await asyncio.wait_for(_request(provider, test=test), timeout=30)
    except Exception as exc:
        # Never echo exception messages: they can contain credentials or response bodies.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        return f"❌ DeepSeek: {detail}. Другие провайдеры не вызывались."


async def _request(provider, *, test):
    base = provider.base_url.rstrip("/")
    headers = {**provider.default_headers, "Authorization": f"Bearer {provider.api_key}"}
    async with proxy_utils.llm_http_client() as client:
        if not test:
            response = await client.get(base + "/user/balance", headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            balances = [f"{item['total_balance']} {item['currency']}" for item in data.get("balance_infos", [])]
            return ("💰 DeepSeek · общий баланс\n" + ("\n".join(balances) or "Баланс не возвращён")
                    + "\nДоступ к запросам: " + ("есть" if data.get("is_available") else "нет"))
        model = TEST_MODEL
        models = await client.get(base + "/models", headers=headers, timeout=10)
        models.raise_for_status()
        if model not in {item.get("id") for item in models.json().get("data", [])}:
            return f"❌ Модель {model} недоступна в аккаунте DeepSeek. Платный тест не отправлен."
        started = time.monotonic()
        response = await client.post(base + "/chat/completions", headers=headers, timeout=20, json={
            "model": model,
            "messages": [{"role": "user", "content": "Ответь только: Связь работает"}],
            "max_tokens": 256,
            "stream": False,
            "thinking": {"type": "disabled"},
        })
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage") or {}
        answer = str(data["choices"][0]["message"].get("content") or "").strip()
        state = "✅" if answer else "⚠️ Пустой ответ."
        return (f"{state} DeepSeek · {data.get('model', model)}\n"
                f"Ответ: {answer[:600] or 'нет текста'}\nВремя: {time.monotonic() - started:.1f} с\n"
                f"Токены: вход {usage.get('prompt_tokens', '?')}, выход {usage.get('completion_tokens', '?')}\n"
                "Стоимость этого запроса API не сообщает; баланс доступен отдельной кнопкой.")
