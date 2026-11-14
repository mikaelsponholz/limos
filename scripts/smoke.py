"""Run the live backend: python scripts/smoke.py --base-url http://localhost:8000."""

import argparse
import json
import sys
from pathlib import Path
from time import monotonic, sleep

import httpx

# Support direct-script invocation as well as python -m scripts.smoke.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.schemas import Audit, RequestCreated, RequestDetail


def run(client: httpx.Client) -> int:
    response = client.post("/requests", json={"preset_id": "restaurant_weekly"})
    response.raise_for_status()
    created = RequestCreated.model_validate_json(response.text)
    deadline = monotonic() + 120
    while True:
        response = client.get(f"/requests/{created.request_id}")
        response.raise_for_status()
        detail = RequestDetail.model_validate_json(response.text)
        if detail.status != "RUNNING" or monotonic() >= deadline:
            break
        sleep(1)
    response = client.get(f"/audit/{created.request_id}")
    response.raise_for_status()
    audit = Audit.model_validate_json(response.text)
    result = detail.order or detail.quote
    print(
        json.dumps(
            {
                "status": detail.status,
                "lines": len(result.lines) if result else 0,
                "total": result.total if result else None,
                "delivery_date": result.delivery_date.isoformat() if result else None,
                "order_deadline": result.order_deadline.isoformat()
                if result and result.order_deadline
                else None,
                "rules_fired": [rule.rule_id for rule in detail.quote.policy.rules_fired]
                if detail.quote
                else [],
                "chain_valid": audit.chain_valid,
            },
            separators=(",", ":"),
        )
    )
    return (
        0
        if detail.status in {"ORDERED", "WAITING_APPROVAL"}
        and audit.chain_valid
        and audit.request_id == created.request_id
        else 1
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    try:
        with httpx.Client(base_url=args.base_url.rstrip("/") + "/", timeout=10.0) as client:
            return run(client)
    except httpx.HTTPStatusError as exc:
        print(f"Smoke failed: backend returned HTTP {exc.response.status_code}.", file=sys.stderr)
    except httpx.RequestError:
        print("Smoke failed: backend request failed or timed out.", file=sys.stderr)
    except ValueError:
        print("Smoke failed: invalid URL or backend response.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
