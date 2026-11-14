"""Resolve intent and expand events before deterministic purchasing and reorder paths."""

import asyncio
import json

from agent.constraints import item_constraints
from agent.exceptions import ItemPlan
from agent.expansion import build_event_items, expand_event
from agent.intent import TextFacts, helsinki_today, parse_text, resolve_request
from agent.llm import LLMOutputError
from agent.loop import run_loop
from agent.purchasing import create_options, resolve_item
from agent.question import apply_answer, choices_and_default, missing_field, word_question
from gateway import customers, tools
from gateway.api.dependencies import Resources
from gateway.audit import append_step
from gateway.errors import GatewayError
from gateway.schemas import (
    Error,
    IntentItem,
    Offer,
    OffersBody,
    OrderBody,
    Question,
    Quote,
    QuoteBody,
    QuoteLine,
    RequestApprovalBody,
    RequestCreate,
    RequestDetail,
    RequestInput,
    SearchBody,
    StepType,
)

AGENT_ID = "limos-runner"


def prepare(body: RequestCreate, env: Resources) -> tuple[RequestDetail, RequestCreate]:
    customer = (
        customers.get_customer(body.customer_id, env.store, **env.customer_paths)
        if body.customer_id
        else None
    )
    now = env.now()
    resolution = resolve_request(body, customer, None, helsinki_today(now))
    resolved = resolution.request
    buyer = resolved.buyer_id
    if buyer is None:
        raise GatewayError(422, "BUYER_REQUIRED", "A buyer is required.")
    env.permit(buyer, AGENT_ID)
    detail = RequestDetail(
        request_id=env.new_id("r_"),
        buyer_id=buyer,
        customer_id=resolved.customer_id,
        preset_id=resolved.preset_id,
        status="RUNNING",
        created_at=now,
        input=RequestInput(text=resolved.text, image_url=None),
        intent=resolution.intent,
        question=None,
        steps=[],
        candidates=[],
        quote=None,
        suggestions=[],
        approval_id=None,
        order=None,
        error=None,
    )
    env.store.save_request(detail)
    # Preserve which fields the buyer supplied until text precedence is resolved.
    return detail, body.model_copy(deep=True)


def answer_question(
    request_id: str, question_id: str, answer: str | None, env: Resources
) -> RequestCreate:
    with env.store.transaction():
        detail = tools.request_detail(env, request_id, env.request_buyer(request_id))
        question = detail.question
        if (
            detail.status != "NEEDS_INPUT"
            or question is None
            or question.question_id != question_id
        ):
            raise GatewayError(409, "NO_OPEN_QUESTION", "There is no matching open question.")
        requested = next(
            step for step in reversed(detail.steps) if step.type == "CLARIFICATION_REQUESTED"
        )
        body = RequestCreate.model_validate_json(json.dumps(requested.detail["resolved_request"]))
        customer = (
            customers.get_customer(detail.customer_id, env.store, **env.customer_paths)
            if detail.customer_id
            else None
        )
        try:
            body, gaps, answer_model = apply_answer(
                body, detail.intent, question, answer, customer, env.llm, helsinki_today(env.now())
            )
        except LLMOutputError:
            detail.status = "FAILED"
            detail.question = None
            detail.error = Error(
                code="LLM_OUTPUT_ERROR",
                message="The answer parser returned invalid output after repair.",
            )
            env.store.save_request(detail)
            append_step(
                env.store,
                request_id,
                "gateway",
                "ERROR",
                detail.error.message,
                {"code": detail.error.code},
                None,
                env.now(),
            )
        else:
            detail.intent.filled_gaps = gaps
            detail.question = None
            detail.status = "RUNNING"
            env.store.save_request(detail)
            append_step(
                env.store,
                request_id,
                "user",
                "CLARIFICATION_ANSWERED",
                "Clarifying question answered",
                {
                    "question_id": question_id,
                    "field": question.field,
                    "used_default": answer is None,
                },
                None,
                env.now(),
            )
            if answer_model is not None:
                append_step(
                    env.store,
                    request_id,
                    "agent",
                    "INTENT_PARSED",
                    "Resolved what to order from your answer",
                    {},
                    answer_model,
                    env.now(),
                )
    if detail.status == "FAILED" and detail.error is not None:
        raise GatewayError(422, detail.error.code, detail.error.message)
    return body


async def run(
    request_id: str, body: RequestCreate, env: Resources, *, resume: bool = False
) -> None:
    detail = tools.request_detail(env, request_id, env.request_buyer(request_id))

    def log(kind: StepType, title: str) -> None:
        append_step(env.store, request_id, "agent", kind, title, {}, None, env.now())

    try:
        if not resume:
            append_step(
                env.store,
                request_id,
                "user",
                "REQUEST_RECEIVED",
                "Request received",
                {},
                None,
                env.now(),
            )
        if detail.customer_id and not resume:
            log("CUSTOMER_LOADED", "Customer profile loaded")
        parsed_text = False
        facts: TextFacts | None = None
        model_id: str | None = None
        if body.text and not resume:
            llm = env.llm
            if llm is None:
                raise GatewayError(
                    503, "AGENT_NOT_AVAILABLE", "Text parsing needs the Gemini agent."
                )
            today = helsinki_today(env.now())
            parsed = parse_text(llm, body.text, today)
            facts, model_id = parsed.value, parsed.model_id
            parsed_text = True
        customer = (
            customers.get_customer(detail.customer_id, env.store, **env.customer_paths)
            if detail.customer_id
            else None
        )
        resolution = resolve_request(body, customer, facts, helsinki_today(env.now()))
        body = resolution.request
        if resume:
            previous = {gap.field: gap for gap in detail.intent.filled_gaps}
            previous.update(
                {
                    gap.field: gap
                    for gap in resolution.intent.filled_gaps
                    if gap.field not in previous
                }
            )
            resolution.intent.filled_gaps = list(previous.values())
            resolution.intent.assumptions = detail.intent.assumptions
        detail.intent = resolution.intent
        env.store.save_request(detail)
        if parsed_text:
            append_step(
                env.store,
                request_id,
                "agent",
                "INTENT_PARSED",
                "Resolved the buyer's text and requirements",
                {
                    "text_fields": [
                        gap.field for gap in detail.intent.filled_gaps if gap.source == "text"
                    ]
                },
                model_id,
                env.now(),
            )
        elif not resume:
            log("INTENT_PARSED", "Item quantities and requirements resolved")
        asked_before = any(step.type == "CLARIFICATION_REQUESTED" for step in detail.steps)
        field = missing_field(detail.intent, body.reorder_of)
        # Preserve pre-agent item-list and reorder behaviour when no LLM is configured.
        if field is not None and not asked_before and env.llm is not None:
            choices, default = choices_and_default(
                field, detail.intent, customer, helsinki_today(env.now())
            )
            event_type = detail.intent.event.type if detail.intent.event else None
            if any(
                gap.field == "event.type" and gap.source == "default"
                for gap in detail.intent.filled_gaps
            ):
                event_type = None
            wording = word_question(env.llm, field, event_type, choices)
            detail.question = Question(
                question_id=env.new_id("qn_"),
                field=field,
                text=wording.text,
                choices=choices,
                default=default,
                asked_at=env.now(),
            )
            detail.status = "NEEDS_INPUT"
            with env.store.transaction():
                env.store.save_request(detail)
                append_step(
                    env.store,
                    request_id,
                    "agent",
                    "CLARIFICATION_REQUESTED",
                    wording.text,
                    {
                        "field": field,
                        "fallback_used": wording.fallback_used,
                        "resolved_request": body.model_dump(mode="json"),
                    },
                    wording.model,
                    env.now(),
                )
            return
        placeholder_event = any(
            gap.field == "event.type" and gap.source == "default"
            for gap in detail.intent.filled_gaps
        )
        if (
            detail.intent.event is not None
            and (detail.intent.dishes or not placeholder_event)
            and not body.reorder_of
            and env.llm is not None
        ):
            expanded = expand_event(
                env.llm,
                detail.intent.event,
                detail.intent.dishes,
                detail.intent.preferences,
                detail.intent.exclude_allergens,
            )
            result = build_event_items(
                expanded.value,
                detail.intent.event,
                detail.intent.dishes,
                detail.intent.preferences,
                detail.intent.exclude_allergens,
                waste_percent=env.permit(detail.buyer_id, AGENT_ID).waste_percent,
                explicit_items=detail.intent.items,
            )
            detail.intent.dishes = result.dishes
            detail.intent.items = result.items
            detail.intent.assumptions.extend(result.assumptions)
            env.store.save_request(detail)
            append_step(
                env.store,
                request_id,
                "agent",
                "EVENT_EXPANDED",
                "Expanded event dishes into purchase items",
                {"dish_count": len(result.dishes), "item_count": len(result.items)},
                expanded.model_id,
                env.now(),
            )
            if not detail.intent.items:
                raise GatewayError(
                    422, "NO_EVENT_ITEMS", "Event expansion produced no valid purchase items."
                )
        lines: list[QuoteLine] = []
        if body.reorder_of:
            if detail.customer_id is None:
                raise GatewayError(422, "CUSTOMER_REQUIRED", "A reorder needs a customer.")
            customer = customers.get_customer(detail.customer_id, env.store, **env.customer_paths)
            based_on, reorder_lines = customers.resolve_reorder(
                customer, body.reorder_of, body.event.guests if body.event else None
            )
            detail.intent.based_on = based_on
            for index, line in enumerate(reorder_lines, 1):
                if env.catalog.lookup(line.product_id) is None:
                    log("EXCEPTION", f"Skipped missing product {line.product_id}")
                    continue
                item_id = f"it_{index}"
                detail.intent.items.append(
                    IntentItem(
                        item_id=item_id,
                        description=line.name,
                        description_fi=line.name,
                        quantity=float(line.quantity_sales_units),
                        unit="pcs",
                        constraints=item_constraints(
                            detail.intent.preferences, detail.intent.exclude_allergens
                        ),
                        used_in=[],
                    )
                )
                lines.append(
                    QuoteLine(
                        item_id=item_id,
                        product_id=line.product_id,
                        quantity_sales_units=float(line.quantity_sales_units),
                    )
                )
        elif not detail.intent.items:
            raise GatewayError(
                503,
                "AGENT_NOT_AVAILABLE",
                "Text and event requests need the Gemini agent, which is not available yet.",
            )
        env.store.save_request(detail)
        if body.reorder_of:
            log("CANDIDATES", f"Resolved {len(lines)} products from the past order")
        agent_loop = not body.reorder_of and env.llm is not None
        plans: list[ItemPlan] = []
        alternative_ids: list[str] = []
        if not body.reorder_of and not agent_loop:
            for item in detail.intent.items:
                tools.tool_step(env, request_id, "search_products")
                shortlist = tools.search_products(
                    SearchBody(
                        buyer_id=detail.buyer_id,
                        agent_id=AGENT_ID,
                        query=item.description,
                        constraints=item.constraints,
                        exclude_allergens=detail.intent.exclude_allergens,
                        limit=5,
                    ),
                    env,
                )
                tools.tool_step(env, request_id, "check_offers")
                offers = await tools.check_offers(
                    OffersBody(
                        buyer_id=detail.buyer_id,
                        agent_id=AGENT_ID,
                        product_ids=[p.product_id for p in shortlist.products],
                        item_id=item.item_id,
                        quantity=item.quantity,
                        unit=item.unit,
                        requested_date=detail.intent.deliver_by,
                        constraints=list(
                            dict.fromkeys(
                                [
                                    *item.constraints,
                                    *[
                                        p.key
                                        for p in detail.intent.preferences
                                        if p.level == "require"
                                    ],
                                ]
                            )
                        ),
                        exclude_allergens=detail.intent.exclude_allergens,
                    ),
                    env,
                )
                detail.candidates.extend(offers)
                env.store.save_request(detail)
                log("CANDIDATES", f"Checked {len(offers)} offers for {item.description}")
                offers.sort(key=lambda offer: offer.product_id)
                if not offers:
                    raise GatewayError(
                        404, "NO_CANDIDATES", "No products match the requested item."
                    )

                async def fetch(ids: list[str], item: IntentItem = item) -> list[Offer]:
                    tools.tool_step(env, request_id, "check_offers")
                    return await tools.check_offers(
                        OffersBody(
                            buyer_id=detail.buyer_id,
                            agent_id=AGENT_ID,
                            product_ids=ids,
                            item_id=item.item_id,
                            quantity=item.quantity,
                            unit=item.unit,
                            requested_date=detail.intent.deliver_by,
                            constraints=list(
                                dict.fromkeys(
                                    [
                                        *item.constraints,
                                        *[
                                            p.key
                                            for p in detail.intent.preferences
                                            if p.level == "require"
                                        ],
                                    ]
                                )
                            ),
                            exclude_allergens=detail.intent.exclude_allergens,
                        ),
                        env,
                    )

                def search(query: str, item: IntentItem = item) -> list[str]:
                    return [
                        product.product_id
                        for product in tools.search_products(
                            SearchBody(
                                buyer_id=detail.buyer_id,
                                agent_id=AGENT_ID,
                                query=query,
                                constraints=item.constraints,
                                exclude_allergens=detail.intent.exclude_allergens,
                                limit=5,
                            ),
                            env,
                        ).products
                    ]

                plan = await resolve_item(item, offers, detail, env, fetch, search, None)
                plans.append(plan)
                for planned_line in plan.recommended:
                    log(
                        "CHOICE",
                        f"Selected product {planned_line.product_id} for {item.description}",
                    )
        if agent_loop:
            llm = env.llm
            assert llm is not None
            quote = await asyncio.to_thread(run_loop, llm, detail, env, alternative_ids)
        elif plans:

            async def create(body: QuoteBody) -> Quote:
                tools.tool_step(env, request_id, "request_quote")
                return await tools.request_quote(body, env)

            quote = await create_options(
                detail, detail.intent.items, plans, AGENT_ID, create, alternative_ids
            )
        else:
            tools.tool_step(env, request_id, "request_quote")
            quote = await tools.request_quote(
                QuoteBody(
                    buyer_id=detail.buyer_id,
                    agent_id=AGENT_ID,
                    request_id=request_id,
                    requested_date=detail.intent.deliver_by,
                    lines=lines,
                    budget=detail.intent.budget,
                ),
                env,
            )
        if body.reorder_of:
            detail.candidates = quote.lines
        if not agent_loop:
            missing_preferences = [
                rule.description
                for rule in quote.policy.rules_fired
                if rule.rule_id == "PREFERENCE_REQUIRED_UNMET"
            ]
            if missing_preferences:
                tools.set_agent_explanation(quote, " ".join(missing_preferences), env)
        detail.quote = quote
        detail.status = "BLOCKED" if quote.policy.outcome == "BLOCK" else "RUNNING"
        env.store.save_request(detail)
        if quote.policy.outcome == "AUTO":
            tools.tool_step(env, request_id, "place_order")
            tools.place_order(
                OrderBody(
                    buyer_id=detail.buyer_id,
                    agent_id=quote.agent_id,
                    quote_id=quote.quote_id,
                    idempotency_key=f"{request_id}-{quote.quote_id}",
                ),
                env,
            )
        elif quote.policy.outcome == "NEEDS_APPROVAL":
            tools.tool_step(env, request_id, "request_approval")
            tools.request_approval(
                quote.quote_id,
                RequestApprovalBody(
                    buyer_id=detail.buyer_id,
                    agent_id=quote.agent_id,
                    explanation=quote.agent_explanation
                    or "Human approval is required: "
                    + "; ".join(rule.description for rule in quote.policy.rules_fired),
                    alternative_quote_ids=alternative_ids or None,
                ),
                env,
            )
        else:
            detail.status = "BLOCKED"
            env.store.save_request(detail)
    except Exception as exc:
        detail = env.store.get_request(request_id, detail.buyer_id) or detail
        detail.status = "FAILED"
        if isinstance(exc, GatewayError):
            detail.error = Error(code=exc.code, message=exc.message)
        elif isinstance(exc, LLMOutputError):
            detail.error = Error(
                code="LLM_OUTPUT_ERROR",
                message="The agent returned invalid output after repair.",
            )
        else:
            detail.error = Error(
                code="INTERNAL_ERROR", message="The request could not be completed."
            )
        append_step(
            env.store,
            request_id,
            "gateway",
            "ERROR",
            detail.error.message,
            {"code": detail.error.code},
            None,
            env.now(),
        )
        env.store.save_request(detail)
