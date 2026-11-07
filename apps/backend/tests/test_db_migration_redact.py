from app.features.db_migration.redact import MASK, clean


def test_known_values_go_as_a_whole():
    assert clean("failed with abcd1234 and abcd", ["abcd1234", "abcd"]) == f"failed with {MASK} and {MASK}"


def test_short_values_do_not_mask_ordinary_text():
    assert clean("a table failed", ["a"]) == "a table failed"


def test_known_forms_are_hidden():
    text = ("key AKIAABCDEFGHIJKLMNOP url postgres://u:pw1@host/db password=pw2 secret: pw3 Bearer pw4\n"
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY----- end")
    result = clean(text)

    for hidden in ("AKIAABCDEFGHIJKLMNOP", "pw1", "pw2", "pw3", "pw4", "abc"):
        assert hidden not in result
    assert "postgres://u:" in result and "@host/db" in result and result.endswith("end")


def test_references_stay_readable():
    assert clean("missing secret://mig/none") == "missing secret://mig/none"
