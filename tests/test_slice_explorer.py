"""The slice explorer demo: real analyser output mapped onto source lines."""

import json
import urllib.request

from demo.slice_explorer import APPS, SliceExplorer, build_app_data


def test_every_app_builds_with_consistent_line_mappings():
    for name in APPS:
        data = build_app_data(name)
        assert data["sites"], name
        span_names = {s["q"] for s in data["spans"]}
        for site in data["sites"]:
            # the anchor sits on a real line of the file
            assert site["line"] is not None and 1 <= site["line"] <= len(data["lines"])
            # every slice member maps to a highlightable span
            assert set(site["members"]) <= span_names, (name, site["id"])
            # every referenced constant maps to a line
            assert set(site["constants"]) <= set(data["constants"]), site["id"]


def test_order_slice_reaches_the_policies():
    data = build_app_data("order")
    (status_site,) = [s for s in data["sites"]
                      if s["function"] == "OrderService._ev"]
    assert status_site["event_type"] == "order.status"
    assert status_site["policies"]["direct"], "policies must be named"
    # the constructor is in the slice (state flows through self._emit)
    assert "service.OrderService.__init__" in status_site["members"]


def test_http_serves_page_and_data():
    explorer = SliceExplorer()
    url = explorer.start(port=0)
    try:
        with urllib.request.urlopen(url + "/", timeout=5) as response:
            assert "slice explorer" in response.read().decode()
        with urllib.request.urlopen(url + "/api/app/ticketing", timeout=5) as response:
            data = json.loads(response.read().decode())
        assert len(data["sites"]) == 5
    finally:
        explorer.stop()
