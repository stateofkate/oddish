"""Preview-owned API keys and their owner rows survive incremental deploys.

The PostgreSQL fixture is a fresh local cluster. These tests never use a live
ODDISH_DATABASE_URL. Parent credentials are disabled once when Neon makes a
child; keys made in that child retain their IDs, hashes, and owner links.
"""

import asyncio

import pytest

from tests.test_preview_bootstrap_fingerprint import (
    _load_bootstrap,
    _run_sql,
    _set_branch,
    _snapshot,
)
from tests.test_preview_bootstrap_fingerprint import (
    isolated_postgres as isolated_postgres,  # noqa: PLC0414
)
from tests.test_preview_bootstrap_fingerprint import (
    preview_database as preview_database,  # noqa: PLC0414
)


def test_owned_credentials_and_fk_parents_survive_new_processes_and_migrations(
    monkeypatch, preview_database
):
    module, url = preview_database
    asyncio.run(module._initialize_clone(url, "br-preview", "br-staging"))
    asyncio.run(
        _run_sql(
            module,
            url,
            """
            CREATE TABLE organizations (id text PRIMARY KEY, name text, settings jsonb);
            CREATE TABLE users (id text PRIMARY KEY, org_id text NOT NULL REFERENCES organizations(id), name text);
            ALTER TABLE api_keys ADD COLUMN org_id text REFERENCES organizations(id);
            ALTER TABLE api_keys ADD COLUMN created_by_user_id text REFERENCES users(id);
            ALTER TABLE api_keys ADD COLUMN key_hash text UNIQUE;
            ALTER TABLE api_keys ADD COLUMN scope text;
            INSERT INTO organizations VALUES ('preview-org', 'My org', '{"budget": 25}');
            INSERT INTO users VALUES ('preview-user', 'preview-org', 'My name');
            INSERT INTO api_keys (id, is_active, org_id, created_by_user_id, key_hash, scope)
                VALUES ('preview-key', true, 'preview-org', 'preview-user', 'saved-key-hash', 'tasks');
            """,
        )
    )
    before = asyncio.run(_snapshot(module, url))
    _set_branch(monkeypatch)
    monkeypatch.setenv("ODDISH_DATABASE_URL", url)

    async def verify(url):
        pass

    for _deploy in range(2):
        # A new module instance cannot use process state to skip initialization.
        module = _load_bootstrap()

        def upgrade(project, module=module):
            asyncio.run(
                _run_sql(
                    module,
                    url,
                    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS tier text NOT NULL DEFAULT 'standard'",
                )
            )

        monkeypatch.setattr(module, "_upgrade_head", upgrade)
        monkeypatch.setattr(module, "_assert_model_schema", verify)
        module.main()

    for row in before["api_keys"]:
        row["tier"] = "standard"
    assert asyncio.run(_snapshot(module, url)) == before
    key = next(row for row in before["api_keys"] if row["id"] == "preview-key")
    assert key["is_active"] is True
    assert key["key_hash"] == "saved-key-hash"
    assert key["created_by_user_id"] == "preview-user"

    module = _load_bootstrap()

    def fail(project):
        raise RuntimeError("new migration failed")

    monkeypatch.setattr(module, "_upgrade_head", fail)
    with pytest.raises(RuntimeError, match="no reset was run"):
        module.main()
    assert asyncio.run(_snapshot(module, url)) == before
