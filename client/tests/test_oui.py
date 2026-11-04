"""Tests for OUI manufacturer lookup."""
from shesha.oui import manufacturer, _is_locally_administered


def test_known_oui():
    # Actiontec
    assert manufacturer("10:78:5b:fd:b6:26") != ""


def test_locally_administered():
    assert _is_locally_administered("0e:78:5b:fd:b6:26") is True
    assert _is_locally_administered("10:78:5b:fd:b6:26") is False
    assert manufacturer("0e:78:5b:fd:b6:26") == "Private MAC"


def test_unknown_oui():
    # ff has LA bit set
    result = manufacturer("ff:ee:dd:cc:bb:aa")
    assert result == "Private MAC"


def test_empty_mac():
    assert manufacturer("") == ""
    assert manufacturer("aa:bb") == ""
