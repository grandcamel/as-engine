import re

import as_engine


def test_version_is_pep440_like():
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", as_engine.__version__)


def test_all_exports_exist():
    for name in as_engine.__all__:
        assert hasattr(as_engine, name)
