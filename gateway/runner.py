"""Resolve intent and expand events before deterministic purchasing and reorder paths."""

import asyncio
import json

from agent.constraints import item_constraints
from agent.errors import error_code
from agent.exceptions import ItemPlan
from agent.expansion import build_event_items, expand_event, propose_dishes
from agent.intent import (
    TextFacts,
    helsinki_today,
    parse_text,
    resolve_request,
    revision_context,
)
from agent.llm import LLMOutputError
from agent.loop import run_loop
from agent.pipeline import run_pipeline
from agent.purchasing import create_options, resolve_item
from agent.question import apply_answer, choices_and_default, missing_field, word_question
from agent.suggestions import propose_suggestions
from gateway import customers, quotes, tools
from gateway.api.dependencies import Resources
from gateway.audit import append_step
from gateway.errors import GatewayError
from gateway.schemas import (
    Dish,
    Error,
    EventDish,
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


REVISABLE = {"NEEDS_INPUT", "WAITING_APPROVAL", "BLOCKED", "REJECTED", "FAILED"}


def _revised_request(body: RequestCreate, env: Resources) -> RequestDetail | None:
    if not body.revises:
        return None
    buyer = env.store.request_buyer(body.revises)
    previous = env.store.get_request(body.revises, buyer) if buyer else None
    if previous is None or (body.buyer_id and body.buyer_id != previous.buyer_id):
        raise GatewayError(404, "NOT_FOUND", "Request to revise not found.")
    if body.customer_id and body.customer_id != previous.customer_id:
        raise GatewayError(404, "NOT_FOUND", "Request to revise not found.")
    if previous.status not in REVISABLE:
        raise GatewayError(
            409,
            "NOT_REVISABLE",
            "This request is still running or already ordered, so it cannot be changed.",
        )
    return previous


def _replace(previous: RequestDetail, new_id: str, env: Resources) -> None:
    """Close the earlier request so its quote can never be ordered next to the new one."""
    title = f"Replaced by the changed request {new_id}"
    approval = (
        env.store.get_approval(previous.approval_id, previous.buyer_id)
        if previous.approval_id
        else None
    )
    rejected = False
    if approval is not None and approval.status == "PENDING":
        try:
            quotes.reject(
                approval.approval_id,
                "buyer",
                title,
                env.now(),
                store=env.store,
                buyer_id=previous.buyer_id,
            )
            rejected = True
        except GatewayError:
            # An expired or already decided quote cannot be ordered either.
            pass
    previous = env.store.get_request(previous.request_id, previous.buyer_id) or previous
    previous.status = "REJECTED"
    previous.question = None
    for suggestion in previous.suggestions:
        if suggestion.status == "OFFERED":
            suggestion.status = "DECLINED"
    if previous.quote is not None:
        previous.quote = env.store.get_quote(previous.quote.quote_id, previous.buyer_id)
    env.store.save_request(previous)
    if not rejected:
        append_step(
            env.store,
            previous.request_id,
            "user",
            "REJECTED",
            title,
            {"replaced_by": new_id},
            None,
            env.now(),
        )


def prepare(body: RequestCreate, env: Resources) -> tuple[RequestDetail, RequestCreate]:
    previous = _revised_request(body, env)
    if previous is not None:
        body = body.model_copy(
            update={
                "buyer_id": body.buyer_id or previous.buyer_id,
                "customer_id": body.customer_id or previous.customer_id,
            }
        )
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
    if previous is not None:
        with env.store.transaction():
            env.store.save_request(detail)
            _replace(previous, detail.request_id, env)
    else:
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
            known = (
                customers.get_customer(detail.customer_id, env.store, **env.customer_paths)
                if detail.customer_id
                else None
            )
            revised = env.store.get_request(body.revises, detail.buyer_id) if body.revises else None
            parsed = parse_text(
                llm,
                body.text,
                today,
                known,
                revision_context(revised) if revised else None,
            )
            if parsed.repaired and env.settings.agent_mode == "pipeline":
                append_step(
                    env.store,
                    request_id,
                    "agent",
                    "EXCEPTION",
                    "Repaired intent output",
                    {"code": "LLM_OUTPUT_ERROR"},
                    parsed.model_id,
                    env.now(),
                )
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
                        "code": "LLM_ERROR" if wording.fallback_used else None,
                        "resolved_request": body.model_dump(mode="json"),
                    },
                    wording.model,
                    env.now(),
                )
            return
        pipeline = env.llm is not None and env.settings.agent_mode == "pipeline"
        search_terms: dict[str, list[str]] = {}
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
            if not detail.intent.dishes and not detail.intent.items:
                proposed = propose_dishes(
                    env.llm,
                    detail.intent.event,
                    detail.intent.preferences,
                    detail.intent.exclude_allergens,
                )
                names = proposed.value.dishes
                detail.intent.dishes = [
                    Dish(dish_id=f"d_{index}", name=name, servings=detail.intent.event.guests)
                    for index, name in enumerate(names, 1)
                ]
                if body.event is not None:
                    body.event.dishes = [
                        EventDish(name=dish.name, servings=dish.servings)
                        for dish in detail.intent.dishes
                    ]
                detail.intent.assumptions.append(
                    f"No dishes were named; proposed {', '.join(names[:-1])} and {names[-1]}. "
                    "Change them before approving."
                )
                env.store.save_request(detail)
            expanded = expand_event(
                env.llm,
                detail.intent.event,
                detail.intent.dishes,
                detail.intent.preferences,
                detail.intent.exclude_allergens,
                with_search_terms=pipeline,
            )
            if expanded.repaired and pipeline:
                append_step(
                    env.store,
                    request_id,
                    "agent",
                    "EXCEPTION",
                    "Repaired expansion output",
                    {"code": "LLM_OUTPUT_ERROR"},
                    expanded.model_id,
                    env.now(),
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
            search_terms = result.search_terms
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
        alternative_ids: list[str] = []
        if pipeline:
            assert env.llm is not None
            pipeline_quote = await run_pipeline(
                env.llm, detail, body, env, search_terms, alternative_ids
            )
            if pipeline_quote is None:
                return
            quote = pipeline_quote
        else:
            lines: list[QuoteLine] = []
            if body.reorder_of:
                if detail.customer_id is None:
                    raise GatewayError(422, "CUSTOMER_REQUIRED", "A reorder needs a customer.")
                customer = customers.get_customer(
                    detail.customer_id, env.store, **env.customer_paths
                )
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
                    detail, detail.intent.items, plans, AGENT_ID, create, alternative_ids, env
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
        try:
            await propose_suggestions(request_id, env)
        except Exception:  # noqa: BLE001 - ideas are optional; the quote stands without them
            append_step(
                env.store,
                request_id,
                "gateway",
                "EXCEPTION",
                "Skipped optional suggestions",
                {"suggestions": "skipped"},
                None,
                env.now(),
            )
    except Exception as exc:
        detail = env.store.get_request(request_id, detail.buyer_id) or detail
        detail.status = (
            "BLOCKED"
            if isinstance(exc, GatewayError) and exc.code == "NO_USABLE_PRODUCTS"
            else "FAILED"
        )
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
            {"code": error_code(exc), "exception_type": type(exc).__name__},
            None,
            env.now(),
        )
        env.store.save_request(detail)
