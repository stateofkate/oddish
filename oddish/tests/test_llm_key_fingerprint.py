from __future__ import annotations

import hashlib

from oddish.core.llm_key_fingerprint import (
    hash_llm_key,
    platform_key_hash_for_provider,
    provider_key_var,
    trial_llm_key_hash,
)


def test_hash_is_sha256_hex():
    assert hash_llm_key("xai-secret") == hashlib.sha256(b"xai-secret").hexdigest()


def test_platform_hash_uses_the_provider_environment(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-platform")
    assert platform_key_hash_for_provider("xai") == hash_llm_key("xai-platform")


def test_public_xai_hash_uses_separate_platform_key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-private")
    monkeypatch.setenv("XAI_PUBLIC_API_KEY", "xai-public")
    assert platform_key_hash_for_provider("xai-public") == hash_llm_key(
        "xai-public"
    )


def test_public_meta_hash_uses_separate_platform_key(monkeypatch):
    from oddish.config import settings

    monkeypatch.setattr(settings, "meta_api_key", None)
    monkeypatch.setattr(settings, "meta_public_api_key", None)
    monkeypatch.setenv("META_API_KEY", "meta-internal")
    monkeypatch.setenv("META_PUBLIC_API_KEY", "meta-public-key")

    assert provider_key_var("meta-public") == "META_PUBLIC_API_KEY"
    assert platform_key_hash_for_provider("meta-public") == hash_llm_key(
        "meta-public-key"
    )
    assert platform_key_hash_for_provider("meta") == hash_llm_key("meta-internal")


def test_bedrock_hash_uses_bearer_token_not_storage_key(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-token")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "storage-key")
    assert platform_key_hash_for_provider("bedrock") == hash_llm_key("bedrock-token")


def test_byok_hash_takes_precedence_over_platform_key(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-platform")
    assert trial_llm_key_hash("xai", {"XAI_API_KEY": "xai-user"}) == hash_llm_key(
        "xai-user"
    )


def test_unknown_provider_does_not_block_trial_accounting():
    assert platform_key_hash_for_provider("not-a-provider") is None
