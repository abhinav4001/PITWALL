from pitwall.realtime.source import DataSource


def test_data_source_protocol_imports() -> None:
    assert callable(getattr(DataSource, "next_state"))
