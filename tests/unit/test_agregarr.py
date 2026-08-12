"""AgregarrClient tests — the transport contract with a co-managing agregarr.

Response and request shapes mirror agregarr 2.4.2 as recorded from a live instance: `/preexisting`
and `/defaulthubs` answer BARE LISTS, and `/reorder` takes `{libraryId, context, mode, mixedItems}`
and numbers the items itself. Built-in hubs come from `/defaulthubs` because `/hubs/configs` — the
name the OpenAPI spec advertises — is session-authenticated and 307s an API key to `/login`.

Per the testing rules these assert the REQUEST body the client is responsible for — the ordering we
send and the context we send it in — not merely that a POST happened. Sending `context: "library"`
or dropping `mode` would leave a call-count assertion perfectly green while writing the wrong shelf.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from shortlist.engine.clients.agregarr import AgregarrClient, AgregarrError

pytestmark = pytest.mark.integration

BASE = "http://agregarr.test"
API = f"{BASE}/api/v1"

PREEXISTING = [
    {
        "id": "cfg-1",
        "collectionRatingKey": "575662",
        "libraryId": "1",
        "name": "✨ Movies Picked for You",
        "sortOrderHome": 2,
        "configType": "preExisting",
    },
    {
        "id": "cfg-2",
        "collectionRatingKey": "343563",
        "libraryId": "2",
        "name": "Action Shows",
        "sortOrderHome": 5,
        "configType": "preExisting",
    },
]
HUB_CONFIGS = {
    "hubConfigs": [
        {
            "id": "1:movie.recentlyadded",
            "hubIdentifier": "movie.recentlyadded",
            "libraryId": "1",
            "name": "Recently Added Movies",
            "sortOrderHome": 1,
            "configType": "hub",
        }
    ]
}


def client() -> AgregarrClient:
    return AgregarrClient(BASE, "secret-key")


class TestHomeItems:
    @respx.mock
    def test_returns_only_the_requested_library_across_both_endpoints(self):
        respx.get(f"{API}/preexisting").mock(return_value=httpx.Response(200, json=PREEXISTING))
        respx.get(f"{API}/defaulthubs").mock(return_value=httpx.Response(200, json=HUB_CONFIGS))

        items = client().home_items("1")

        # The library-2 collection is excluded; the hub and the library-1 row are not.
        assert [i["id"] for i in items] == ["cfg-1", "1:movie.recentlyadded"]

    @respx.mock
    def test_sends_the_api_key_as_a_header(self):
        route = respx.get(f"{API}/preexisting").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{API}/defaulthubs").mock(return_value=httpx.Response(200, json=HUB_CONFIGS))

        client().home_items("1")

        # X-Api-Key, not Authorization: Bearer — agregarr answers 401 to a bearer token.
        assert route.calls.last.request.headers["X-Api-Key"] == "secret-key"
        assert "authorization" not in route.calls.last.request.headers

    @respx.mock
    def test_reads_built_in_hubs_from_the_api_key_authenticated_route(self):
        # /hubs/configs is SESSION-authenticated and 307s to /login for an API key, so the hubs must
        # come from /defaulthubs. Reading the wrong one loses every built-in hub from the ordering.
        respx.get(f"{API}/preexisting").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{API}/defaulthubs").mock(return_value=httpx.Response(200, json=HUB_CONFIGS["hubConfigs"]))

        items = client().home_items("1")

        assert [i["id"] for i in items] == ["1:movie.recentlyadded"]

    @respx.mock
    def test_defaults_a_missing_config_type(self):
        # configType routes the item inside agregarr's own reorder handler; an item that arrives
        # without one must not be sent back without one.
        respx.get(f"{API}/preexisting").mock(
            return_value=httpx.Response(200, json=[{"id": "x", "collectionRatingKey": "9", "libraryId": "1"}])
        )
        respx.get(f"{API}/defaulthubs").mock(return_value=httpx.Response(200, json={"hubConfigs": []}))

        assert client().home_items("1")[0]["configType"] == "preExisting"


class TestApplyHomeOrder:
    @respx.mock
    def test_posts_the_order_as_home_context_manual_mode(self):
        route = respx.post(f"{API}/reorder").mock(
            return_value=httpx.Response(200, json={"success": True, "totalItemsProcessed": 2})
        )
        ordered = [PREEXISTING[0], HUB_CONFIGS["hubConfigs"][0]]

        processed = client().apply_home_order("1", ordered)

        body = json.loads(route.calls.last.request.content)
        # The fields the client controls. "home" is the shelf we mean; "library" would renumber a
        # different sort key entirely, and mode must be manual for mixedItems to be honoured.
        assert body["libraryId"] == "1"
        assert body["context"] == "home"
        assert body["mode"] == "manual"
        # Order is the payload — agregarr assigns sortOrderHome from the index, so this IS the write.
        assert [i["id"] for i in body["mixedItems"]] == ["cfg-1", "1:movie.recentlyadded"]
        assert processed == 2

    @respx.mock
    def test_sends_the_minimum_because_every_field_sent_overwrites_storage(self):
        """Agregarr merges `{...stored, ...sent}`, so a field we send is a field we CLOBBER.

        Echoing back the whole config we read is therefore the maximum-damage option. We send only
        what the write needs, and the join key is part of that minimum: the type guards are bare
        `'collectionRatingKey' in config` checks, and an item satisfying neither is skipped while
        the request still returns 200 — a write that silently does nothing.
        """
        route = respx.post(f"{API}/reorder").mock(return_value=httpx.Response(200, json={"totalItemsProcessed": 1}))
        fat = {**PREEXISTING[0], "customPoster": "poster.png", "visibilityConfig": {"usersHome": True}}

        client().apply_home_order("1", [fat])

        sent = json.loads(route.calls.last.request.content)["mixedItems"][0]
        assert set(sent) == {"id", "configType", "collectionRatingKey", "position"}
        # Fields we never modelled must not be echoed back over agregarr's own storage.
        assert "customPoster" not in sent and "visibilityConfig" not in sent

    @respx.mock
    def test_keeps_a_hub_identifier_so_hub_items_pass_the_type_guard(self):
        route = respx.post(f"{API}/reorder").mock(return_value=httpx.Response(200, json={"totalItemsProcessed": 1}))

        client().apply_home_order("1", [HUB_CONFIGS["hubConfigs"][0]])

        sent = json.loads(route.calls.last.request.content)["mixedItems"][0]
        assert sent["hubIdentifier"] == "movie.recentlyadded"

    @respx.mock
    def test_preserves_a_stored_position_rather_than_renumbering_it(self):
        # `position` is required by the schema but means nothing to the ordering, which is decided
        # by array index. Overwriting a stored value would be a mutation with no purpose.
        route = respx.post(f"{API}/reorder").mock(return_value=httpx.Response(200, json={"totalItemsProcessed": 2}))
        with_position = {**PREEXISTING[0], "position": 77}
        without = {"id": "cfg-9", "collectionRatingKey": "9", "libraryId": "1", "configType": "preExisting"}

        client().apply_home_order("1", [with_position, without])

        sent = json.loads(route.calls.last.request.content)["mixedItems"]
        assert sent[0]["position"] == 77  # preserved
        assert sent[1]["position"] == 1  # filled in from the index

    @respx.mock
    def test_drops_an_item_with_no_id_instead_of_losing_the_library(self):
        # Agregarr rejects the WHOLE request over one malformed entry, so one bad config must cost
        # one row, not the library's entire ordering.
        route = respx.post(f"{API}/reorder").mock(return_value=httpx.Response(200, json={"totalItemsProcessed": 1}))
        broken = {"collectionRatingKey": "9", "libraryId": "1", "configType": "preExisting"}

        client().apply_home_order("1", [broken, PREEXISTING[0]])

        sent = json.loads(route.calls.last.request.content)["mixedItems"]
        assert [i["id"] for i in sent] == ["cfg-1"]

    @respx.mock
    def test_stamps_the_required_position_on_every_item(self):
        # agregarr's schema requires `position` on each item and rejects the WHOLE request without
        # it — and it serves configs from /preexisting that omit the field, so passing its own
        # payload straight back is a 400. Real instance: 106 of 213 configs had no `position`.
        route = respx.post(f"{API}/reorder").mock(return_value=httpx.Response(200, json={"totalItemsProcessed": 2}))
        no_position = {"id": "cfg-9", "collectionRatingKey": "9", "libraryId": "1", "configType": "preExisting"}

        client().apply_home_order("1", [no_position, PREEXISTING[0]])

        body = json.loads(route.calls.last.request.content)
        assert [i["position"] for i in body["mixedItems"]] == [0, 1]

    @respx.mock
    def test_writes_nothing_when_there_is_nothing_to_order(self):
        route = respx.post(f"{API}/reorder")

        assert client().apply_home_order("1", []) == 0

        assert not route.called

    @respx.mock
    def test_raises_when_agregarr_rejects_the_write(self):
        respx.post(f"{API}/reorder").mock(return_value=httpx.Response(500, text="boom"))

        with pytest.raises(AgregarrError):
            client().apply_home_order("1", [PREEXISTING[0]])


class TestErrors:
    @respx.mock
    def test_reports_a_rejected_key_without_leaking_it(self):
        respx.get(f"{API}/preexisting").mock(return_value=httpx.Response(401))

        with pytest.raises(AgregarrError) as excinfo:
            client().ping()

        # plex-safety rule 9: the key never appears in an exception message.
        assert "rejected the API key" in str(excinfo.value)
        assert "secret-key" not in str(excinfo.value)

    @respx.mock
    def test_reports_an_unreachable_instance_without_leaking_the_url_credentials(self):
        respx.get(f"{API}/preexisting").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(AgregarrError) as excinfo:
            client().ping()

        assert "unreachable" in str(excinfo.value)

    @respx.mock
    def test_raises_on_a_non_json_body(self):
        # A reverse proxy in front of agregarr serving an HTML error page with a 200.
        respx.get(f"{API}/preexisting").mock(return_value=httpx.Response(200, text="<html>nope</html>"))

        with pytest.raises(AgregarrError):
            client().ping()


class TestItGivesUpQuickly:
    """The mirror is optional work at the very end of a run, so a hung agregarr must cost seconds.

    At the shared 30s / 3-attempt default, an instance that accepts connections but never answers
    would add roughly three and a half minutes PER LIBRARY before the run could finish. Nothing here
    is worth that: giving up means the shelf stays contested until the next run re-applies it.
    """

    def test_reads_and_writes_use_the_short_budget(self, monkeypatch):
        calls: list[dict] = []

        def record(*args, **kwargs):
            calls.append(kwargs)
            raise httpx.ConnectError("refused")

        monkeypatch.setattr("shortlist.engine.clients.http_retry.get", record)
        monkeypatch.setattr("shortlist.engine.clients.http_retry.request", record)

        with pytest.raises(AgregarrError):
            client().home_items("1")
        with pytest.raises(AgregarrError):
            client().apply_home_order("1", [PREEXISTING[0]])

        assert calls, "nothing was sent, so no budget was asserted"
        for kwargs in calls:
            assert kwargs["timeout"] == 10.0
            assert kwargs["attempts"] == 2

    def test_the_worst_case_wait_stays_inside_a_minute(self):
        """The numbers above, multiplied out — the reason they were chosen."""
        from shortlist.engine.clients.agregarr import ATTEMPTS, TIMEOUT_S

        reads, writes = 2, 1
        worst_case_per_library = (reads * ATTEMPTS + writes * ATTEMPTS) * TIMEOUT_S

        assert worst_case_per_library <= 60


class TestPing:
    @respx.mock
    def test_reports_how_many_collections_agregarr_knows(self):
        respx.get(f"{API}/preexisting").mock(return_value=httpx.Response(200, json=PREEXISTING))
        respx.get(API).mock(return_value=httpx.Response(200, json={"api": "Agregarr API", "version": "1.0"}))

        assert client().ping() == "Connected to Agregarr API 1.0 — 2 collection(s) known"

    @respx.mock
    def test_still_succeeds_when_the_version_probe_fails(self):
        respx.get(f"{API}/preexisting").mock(return_value=httpx.Response(200, json=[]))
        respx.get(API).mock(side_effect=httpx.ConnectError("refused"))

        assert "Agregarr" in client().ping()
