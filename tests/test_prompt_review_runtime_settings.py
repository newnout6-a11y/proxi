"""Review fixes of the admin settings: the ceiling of the first in-turn keepwarm ping."""
import pytest

import admin
import apierr
import direct
from test_admin import admin_relay  # noqa: F401 - the relay fixture


# -- C2: the first in-turn ping cannot be set later than the transport can use it -------------------------------------------
def test_the_turn_offset_the_admin_accepts_stops_where_the_transport_stops():
    field = admin._schema()["cache_keepwarm_turn_offset"]
    assert field["max"] == int(direct.PING_LATEST) == 275 and field["min"] == 30


def test_a_turn_offset_the_transport_would_pull_back_is_refused(admin_relay):
    status, body, _ = admin_relay.patch({"cache_keepwarm_turn_offset": 276})
    assert status == 400 and body["error"]["param"] == "cache_keepwarm_turn_offset"
    with pytest.raises(apierr.ApiError):
        admin_relay.runtime.admin._validate({"cache_keepwarm_turn_offset": 285})
    for accepted in (30, 200, 275):
        admin_relay.runtime.admin._validate({"cache_keepwarm_turn_offset": accepted})
