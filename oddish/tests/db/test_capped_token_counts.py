from sqlalchemy.dialects import postgresql

from oddish.db.models import INT32_MAX, Base, CappedTokenCount


def _token_columns():
    return [
        (table.name, col)
        for table in Base.metadata.tables.values()
        for col in table.columns
        if col.name.endswith("_tokens")
    ]


def test_capped_token_count_saturates_at_int32():
    # A Sonnet 5.5 kubernetes-rust-rewrite trial reported 2,980,931,441 input
    # tokens; an unchecked INTEGER bind makes Postgres reject the final trial
    # UPDATE and the attempt's result is lost.
    bind = CappedTokenCount().process_bind_param
    assert bind(2_980_931_441, None) == INT32_MAX
    assert bind(INT32_MAX, None) == INT32_MAX
    assert bind(1234, None) == 1234
    assert bind(None, None) is None


def test_integer_token_columns_are_capped_without_schema_change():
    capped = 0
    for table_name, col in _token_columns():
        if isinstance(col.type, CappedTokenCount):
            capped += 1
            # Same DDL as before, so no migration and no table rewrite.
            assert col.type.compile(dialect=postgresql.dialect()) == "INTEGER"
        else:
            assert col.type.compile(dialect=postgresql.dialect()) == "BIGINT", (
                f"{table_name}.{col.name} is a 32-bit column without the cap"
            )
    assert capped == 12
