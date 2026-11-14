"""Refresh an expired human decision once, without ordering or model calls."""

from gateway import quotes, tools
from gateway.api.dependencies import Resources, domain_errors
from gateway.audit import append_step
from gateway.errors import GatewayError
from gateway.offers import price_fixed_lines
from gateway.policy import ItemContext
from gateway.schemas import Error, QuoteBody, QuoteLine
from gateway.store import ApprovalRecord


async def requote_expired(approval: ApprovalRecord, env: Resources) -> None:
    # Commit the log claim before I/O, keeping the stored approval PENDING.
    with env.store.transaction():
        current = env.store.get_approval(approval.approval_id, approval.buyer_id)
        if current is None or current.status != "PENDING":
            return
        if any(
            step.actor == "agent"
            and step.type == "QUOTE_EXPIRED"
            and step.detail.get("approval_id") == approval.approval_id
            for step in env.store.get_steps(approval.request_id, approval.buyer_id)
        ):
            return
        old_options = [
            quotes.get_quote(env.store, key, approval.buyer_id, env.now())
            for key in dict.fromkeys([approval.quote_id, *approval.option_quote_ids])
        ]
        if not any(quote.status == "EXPIRED" for quote in old_options):
            return
        for quote in old_options:
            quote.status = "EXPIRED"
            env.store.save_quote(quote)
        append_step(
            env.store,
            approval.request_id,
            "agent",
            "QUOTE_EXPIRED",
            "The approval expired; refreshing its quote options",
            {"approval_id": approval.approval_id},
            None,
            env.now(),
        )
    try:
        detail = tools.request_detail(env, approval.request_id, approval.buyer_id)
        fresh = []
        options = list(dict.fromkeys([approval.quote_id, *approval.option_quote_ids]))
        # Fetch every option before publishing a replacement approval.
        for quote_id in options:
            old = quotes.get_quote(env.store, quote_id, approval.buyer_id, env.now())
            body = QuoteBody(
                request_id=old.request_id,
                buyer_id=old.buyer_id,
                agent_id=old.agent_id,
                requested_date=old.lines[0].requested_date,
                lines=[
                    QuoteLine(
                        item_id=line.item_id,
                        product_id=line.product_id,
                        quantity_sales_units=line.quantity_sales_units,
                        substitute_for=line.substitute_for,
                        substitute_source=line.substitute_source,
                    )
                    for line in old.lines
                ],
                budget=old.budget.limit if old.budget else None,
                option_label=old.option_label,
            )
            mandate = env.permit(old.buyer_id, old.agent_id)
            context = tools.quote_contexts(body, detail, env)
            # Stored line checks also retain additive tool-client constraints.
            for line in old.lines:
                item = context[line.item_id]
                context[line.item_id] = ItemContext(
                    constraints=list(
                        dict.fromkeys(
                            [
                                *item.constraints,
                                *(
                                    key
                                    for key in line.dietary_check
                                    if key not in item.required_preferences
                                ),
                            ]
                        )
                    ),
                    required_preferences=item.required_preferences,
                    exclude_allergens=item.exclude_allergens,
                )
            with domain_errors():
                lines = await price_fixed_lines(
                    body.lines,
                    buyer_id=old.buyer_id,
                    agent_id=old.agent_id,
                    requested_date=body.requested_date,
                    catalog=env.catalog,
                    valio=env.valio,
                    now=env.now(),
                    needs={line.item_id: line.quantity_requested for line in old.lines},
                    item_context=context,
                    max_overbuy_percent=mandate.max_overbuy_percent,
                    refresh=True,
                )
            fresh.append((old, body, context, mandate, lines))
        with env.store.transaction():
            new_quotes = []
            for old, body, context, mandate, lines in fresh:
                quote = quotes.create_quote(
                    lines,
                    store=env.store,
                    request_id=old.request_id,
                    buyer_id=old.buyer_id,
                    agent_id=old.agent_id,
                    requested_date=body.requested_date,
                    item_context=context,
                    mandate=mandate,
                    now=env.now(),
                    id_generator=lambda: env.new_id("q_"),
                    budget=body.budget,
                    option_label=old.option_label,
                    agent_explanation=old.agent_explanation,
                    previous_prices={
                        (line.item_id, line.product_id): line.unit_price for line in old.lines
                    },
                )
                if quote.policy.outcome == "BLOCK":
                    raise GatewayError(
                        422, "BLOCKED", "A refreshed quote option is blocked by policy."
                    )
                new_quotes.append(quote)
            recommended = new_quotes[0]
            replacement = quotes.request_approval(
                recommended.quote_id,
                recommended.buyer_id,
                approval.agent_id,
                approval.explanation,
                [quote.quote_id for quote in new_quotes[1:]],
                store=env.store,
                now=env.now(),
                id_generator=lambda: env.new_id("a_"),
                allow_auto=True,
            )
            detail = tools.request_detail(env, approval.request_id, approval.buyer_id)
            detail.quote = env.store.get_quote(recommended.quote_id, approval.buyer_id)
            detail.approval_id = replacement.approval_id
            detail.status = "WAITING_APPROVAL"
            detail.error = None
            env.store.save_request(detail)
            append_step(
                env.store,
                approval.request_id,
                "agent",
                "REQUOTED",
                "Refreshed the expired quotes and requested a new human decision",
                {
                    "expired_approval_id": approval.approval_id,
                    "approval_id": replacement.approval_id,
                    "quote_ids": [quote.quote_id for quote in new_quotes],
                },
                None,
                env.now(),
            )
    except Exception as exc:
        # Persist the expiry claim and a safe error, never transport exception text.
        code = exc.code if isinstance(exc, GatewayError) else "REQUOTE_FAILED"
        with env.store.transaction():
            failed = env.store.get_request(approval.request_id, approval.buyer_id)
            if failed:
                failed.status = "FAILED"
                failed.error = Error(
                    code=code, message="Could not refresh the expired quotes. No order was placed."
                )
                env.store.save_request(failed)
            append_step(
                env.store,
                approval.request_id,
                "agent",
                "ERROR",
                "Could not refresh the expired quotes; no order was placed",
                {"approval_id": approval.approval_id, "code": code},
                None,
                env.now(),
            )
