"""Dependency-free wall-clock formatting for console diagnostics, including journal failures."""
import datetime


def timestamp():
    """Local date, milliseconds and explicit UTC offset; not a duration clock."""
    now = datetime.datetime.now().astimezone()
    offset = now.strftime("%z")
    return "[%s %s:%s] " % (now.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (now.microsecond // 1000),
                             offset[:3], offset[3:])
