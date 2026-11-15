"""A buyer can change an open request in chat; the old quote can then never be ordered."""

import json

from gateway.audit import verify_chain
from tests.engine_support import BUYER
from tests.test_e2e import poll
from tests.test_offers import product
from tests.test_pipeline import EXPLANATION, environment, item, picks, start

RECORDS = [product("990001"), product("990002", names={"en": "Invented carrots"})]
CHANGE = {
    "items": [{"description": "carrots", "quantity": 5, "unit": "kg"}],
    "deliver_by": "2026-11-20",
}


def revise(client, previous_id, text="vaihda porkkanoihin, 5 kg", **changes):
    return client.post("/requests", json={"revises": previous_id, "text": text, **changes})


def approve(client, detail):
    quote = detail.quote
    return client.post(
        f"/approvals/{detail.approval_id}/approve",
        json={
            "approver": "invented-approver",
            "quote_id": quote.quote_id,
            "content_hash": quote.content_hash,
            "comment": "ok",
        },
    )


def test_change_replaces_the_open_quote_and_reaches_the_parser(tmp_path):
    outputs = [picks("990001"), EXPLANATION, CHANGE, picks("990002"), EXPLANATION]
    with environment(tmp_path, RECORDS, outputs, approval=True) as client:
        first = start(client)
        assert first.status == "WAITING_APPROVAL"
        response = revise(client, first.request_id)
        assert response.status_code == 202, response.text
        changed = poll(client, response.json()["request_id"])
        assert changed.status == "WAITING_APPROVAL" and changed.buyer_id == BUYER
        assert [line.product_id for line in changed.quote.lines] == ["990002"]

        sent = json.loads(
            client.app.state.llm.calls[2].contents[0].parts[0].text.split("\nReturn JSON")[0]
        )
        assert sent["text"] == "vaihda porkkanoihin, 5 kg"
        assert sent["previous_request"]["items"][0]["description"] == "flour"
        assert sent["previous_request"]["deliver_by"] == "2026-11-20"

        old = client.get(f"/requests/{first.request_id}").json()
        assert old["status"] == "REJECTED" and old["quote"]["status"] == "REJECTED"
        step = old["steps"][-1]
        assert step["type"] == "REJECTED" and changed.request_id in step["detail"]["reason"]
        assert verify_chain(poll(client, first.request_id).steps)
        assert approve(client, first).status_code == 409


def test_a_finished_or_running_request_cannot_be_changed(tmp_path):
    with environment(tmp_path, RECORDS, [picks("990001"), EXPLANATION]) as client:
        ordered = start(client)
        assert ordered.status == "ORDERED"
        response = revise(client, ordered.request_id)
        assert response.status_code == 409 and response.json()["error"]["code"] == "NOT_REVISABLE"


def test_change_needs_text_and_the_same_buyer(tmp_path):
    outputs = [picks("990001"), EXPLANATION]
    with environment(tmp_path, RECORDS, outputs, approval=True) as client:
        first = start(client)
        assert (
            client.post(
                "/requests",
                json={"revises": first.request_id, "items": [item()], "buyer_id": BUYER},
            ).status_code
            == 422
        )
        other = revise(client, first.request_id, buyer_id="invented-other")
        assert other.status_code == 404
        assert revise(client, "r_missing").status_code == 404
        assert poll(client, first.request_id).status == "WAITING_APPROVAL"
