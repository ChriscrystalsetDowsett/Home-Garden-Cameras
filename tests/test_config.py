"""Config loading + fail-fast secret validation."""
import pytest


def test_config_imports_with_real_settings():
    from app import config
    assert config.SERVER_PORT == 8080
    assert config.DEFAULT_RESOLUTION in config.RESOLUTIONS
    assert isinstance(config.ALLOWED_ORIGINS, list)


def test_photo_base_url_normalised():
    from app import config
    assert config.PHOTO_BASE_URL.startswith("https://")
    assert not config.PHOTO_BASE_URL.endswith("/")


def test_validate_rejects_placeholder_api_token(monkeypatch):
    from app import config
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HGC_SKIP_SECRET_CHECK", raising=False)
    monkeypatch.setattr(config, "API_TOKEN", "changeme")
    with pytest.raises(RuntimeError, match="api_token"):
        config._validate()


def test_validate_rejects_empty_secret_key(monkeypatch):
    from app import config
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HGC_SKIP_SECRET_CHECK", raising=False)
    monkeypatch.setattr(config, "SECRET_KEY", "")
    with pytest.raises(RuntimeError, match="secret_key"):
        config._validate()


def test_validate_passes_with_real_secrets(monkeypatch):
    # settings.yaml in this tree carries real generated secrets.
    from app import config
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HGC_SKIP_SECRET_CHECK", raising=False)
    config._validate()


def test_skip_env_var_bypasses_validation(monkeypatch):
    from app import config
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HGC_SKIP_SECRET_CHECK", "1")
    monkeypatch.setattr(config, "API_TOKEN", "")
    config._validate()  # must not raise
