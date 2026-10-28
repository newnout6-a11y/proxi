import orchestrator


def test_trim_counts_bytes_not_characters():
    text = "ж" * 100  # 200 bytes in UTF-8
    assert orchestrator._b(text) == 200
    assert len(orchestrator._trim(text, 100)) == 50
    assert orchestrator._trim(text, 0) == ""
    assert orchestrator._trim("short", 100) == "short"
