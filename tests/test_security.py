from obsidian_sidecar.security import REDACTION, contains_secret, redact_text


def test_redacts_multiple_secret_types() -> None:
    fake_key = "sk-api-" + "abcdefghijklmnopqrstuvwxyz123456"
    text = f"openai {fake_key} and password=correct-horse-battery-staple"
    redacted, kinds = redact_text(text)
    assert redacted.count(REDACTION) == 2
    assert "openai-key" in kinds
    assert "assigned-secret" in kinds
    assert not contains_secret(redacted)


def test_normal_technical_text_is_not_flagged() -> None:
    text = "Configure model=gpt-5.6-luna and verify the responsive navigation."
    assert not contains_secret(text)
    assert redact_text(text) == (text, [])
