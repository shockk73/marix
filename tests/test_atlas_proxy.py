import pytest

from providers.atlas_proxy import (
    apply_proxy_target,
    describe_proxy_url,
    normalize_asn,
    normalize_country,
)


def test_apply_proxy_target_sets_country_and_asn():
    proxy = "http://login__cr.pl:secret@gw.dataimpulse.com:823"
    result = apply_proxy_target(proxy, "at", "8412")
    assert result == "http://login__cr.at;asn.8412:secret@gw.dataimpulse.com:823"


def test_apply_proxy_target_replaces_existing_asn():
    proxy = "http://login__cr.at;asn.8412:secret@gw.dataimpulse.com:823"
    result = apply_proxy_target(proxy, "ch", None)
    assert result == "http://login__cr.ch:secret@gw.dataimpulse.com:823"


def test_describe_proxy_url_hides_credentials():
    status = describe_proxy_url(
        "http://login__cr.at;asn.8412:secret@gw.dataimpulse.com:823",
        "env",
    )
    assert status["country"] == "at"
    assert status["asn"] == "8412"
    assert status["host"] == "gw.dataimpulse.com"
    assert "login" not in str(status)
    assert "secret" not in str(status)


def test_normalize_country_blocks_by_ru():
    with pytest.raises(ValueError):
        normalize_country("ru")
    with pytest.raises(ValueError):
        normalize_country("by")


def test_normalize_asn_requires_digits():
    assert normalize_asn("8412") == "8412"
    with pytest.raises(ValueError):
        normalize_asn("AS8412")
