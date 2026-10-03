import llm_client


def test_openrouter_defaults_route_text_and_vision_to_free_models():
    aliases = llm_client._openrouter_model_aliases({})
    assert aliases
    assert aliases["gpt-oss:120b"] == "openrouter/free"
    assert aliases["gpt-oss:20b"] == "openrouter/free"
    assert aliases["qwen3-vl:235b-instruct"] == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
    assert all(model == "openrouter/free" or model.endswith(":free") for model in aliases.values())


def test_openrouter_explicit_models_remain_authoritative():
    aliases = llm_client._openrouter_model_aliases({
        "OPENROUTER_FAST_MODEL": "explicit-fast:free",
        "OPENROUTER_STRONG_MODEL": "explicit-strong:free",
        "OPENROUTER_CODER_MODEL": "explicit-coder:free",
        "OPENROUTER_VISION_MODEL": "explicit-vision:free",
    })
    assert aliases["gpt-oss:20b"] == "explicit-fast:free"
    assert aliases["gpt-oss:120b"] == "explicit-strong:free"
    assert aliases["qwen3-coder:480b"] == "explicit-coder:free"
    assert aliases["qwen3-vl:235b-instruct"] == "explicit-vision:free"


def test_both_openrouter_accounts_share_aliases_without_changing_native_models(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "openrouter,openrouter2")
    monkeypatch.setattr(llm_client, "_load_provider_env", lambda: {
        "OPENROUTER_BASE_URL": "https://openrouter.test/v1",
        "OPENROUTER_API_KEY": "account-one",
        "OPENROUTER2_API_KEY": "account-two",
    })
    providers = llm_client._build_provider_specs()
    assert [provider.name for provider in providers] == ["openrouter", "openrouter2"]
    for provider in providers:
        assert provider.model_for("gpt-oss:120b") == "openrouter/free"
        assert provider.model_for("qwen3-vl:235b-instruct") == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
        assert provider.model_for("explicit/native-model") == "explicit/native-model"
