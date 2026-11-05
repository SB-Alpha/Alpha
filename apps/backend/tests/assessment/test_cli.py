import pytest

from app.features.assessment import cli


def test_mask_hides_account_network_ids_and_rds_host():
    text = (
        '{"account_id": "111122223333", "image": "111122223333.dkr.ecr.ap-northeast-2.amazonaws.com/app:1", '
        '"vpc": "vpc-0abc1234def567890", "sg": "sg-0123abcd", '
        '"host": "db-alpha.c1x2y3z4.ap-northeast-2.rds.amazonaws.com"}'
    )
    masked = cli.mask(text, "111122223333")
    assert "111122223333" not in masked
    assert "vpc-0abc" not in masked and "sg-0123abcd" not in masked
    assert "rds.amazonaws.com" not in masked
    assert "<account>.dkr.ecr.ap-northeast-2.amazonaws.com/app:1" in masked


def test_no_mask_refuses_to_write_file():
    with pytest.raises(SystemExit):
        cli.main(["--profile", "x", "--aws-region", "ap-northeast-2", "--no-mask", "--out", "x.json"])
