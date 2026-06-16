import logging
from cncwatch.engine import setup_logging


def test_setup_logging_writes_to_file(tmp_path):
    p = tmp_path / "sub" / "wd.log"
    lg = setup_logging(str(p))
    lg.info("hello-from-test")
    for h in lg.handlers:
        h.flush()
    assert p.exists()
    text = p.read_text()
    assert "hello-from-test" in text


def test_setup_logging_is_idempotent(tmp_path):
    p = str(tmp_path / "wd.log")
    setup_logging(p)
    lg = setup_logging(p)
    # only one file handler + one stream handler, not duplicated
    file_handlers = [h for h in lg.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
