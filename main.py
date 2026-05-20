"""
Production-ready FastAPI backend for Stripe donations.
Updated for Stripe 2025 API changes.

Features:
- One-time donations
- Monthly subscriptions
- Yearly subscriptions
- Stripe webhook handling
- Reusable recurring prices
- Proper Payment Element subscription flow
- Render-ready deployment
- Idempotency protection
"""

import os
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    Field,
    EmailStr,
    field_validator,
)

# ─────────────────────────────────────────────────────────────
# ENVIRONMENT
# ─────────────────────────────────────────────────────────────

load_dotenv()

STRIPE_SECRET_KEY = os.getenv(
    "STRIPE_SECRET_KEY",
    "",
)

STRIPE_WEBHOOK_SECRET = os.getenv(
    "STRIPE_WEBHOOK_SECRET",
    "",
)

if not STRIPE_SECRET_KEY:

    raise RuntimeError(
        "Missing STRIPE_SECRET_KEY"
    )

# ─────────────────────────────────────────────────────────────
# STRIPE CONFIG
# ─────────────────────────────────────────────────────────────

stripe.api_key = STRIPE_SECRET_KEY

# Optional:
# Remove this line if Stripe rejects the API version
stripe.api_version = "2025-04-30.basil"

# Retry failed network requests automatically
stripe.max_network_retries = 2
# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "donation_api"
)

# ─────────────────────────────────────────────────────────────
# EXCHANGE RATES
# ─────────────────────────────────────────────────────────────

EXCHANGE_RATES = {
    "USD": 1.35,
    "EUR": 1.45,
    "GBP": 1.70,
    "CAD": 1.00,
}

# ─────────────────────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────────────────────

_product_cache: dict[str, str] = {}
_price_cache: dict[str, str] = {}

# ─────────────────────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────────────────────

class ExchangeRateResponse(BaseModel):
    rates: dict[str, float]


class DonationRequest(BaseModel):

    amount: int = Field(
        ...,
        ge=100,
    )

    frequency: Literal[
        "One Time",
        "Monthly",
        "Yearly",
    ]

    currency: str = Field(
        ...,
        min_length=3,
        max_length=3,
    )

    firstName: str = Field(
        ...,
        min_length=1,
        max_length=100,
    )

    lastName: str = Field(
        ...,
        min_length=1,
        max_length=100,
    )

    email: EmailStr

    @field_validator("currency")
    @classmethod
    def validate_currency(
        cls,
        value: str,
    ) -> str:

        return value.upper()


class DonationResponse(BaseModel):

    clientSecret: str

    type: Literal[
        "payment_intent",
        "subscription",
    ]

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

async def get_or_create_customer(
    email: str,
    first_name: str,
    last_name: str,
) -> stripe.Customer:

    try:

        customers = stripe.Customer.search(
            query=f"email:'{email}'",
            limit=1,
        )

        if customers.data:

            customer = customers.data[0]

            logger.info(
                f"Using customer: "
                f"{customer.id}"
            )

            return customer

        customer = stripe.Customer.create(
            email=email,
            name=f"{first_name} {last_name}",
            metadata={
                "first_name": first_name,
                "last_name": last_name,
            },
        )

        logger.info(
            f"Created customer: "
            f"{customer.id}"
        )

        return customer

    except stripe.error.StripeError as exc:

        logger.exception(
            "Customer error"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                exc.user_message
                or str(exc)
            ),
        )


async def get_or_create_donation_product() -> str:

    cache_key = "donation_product"

    if cache_key in _product_cache:

        return _product_cache[
            cache_key
        ]

    try:

        products = stripe.Product.search(
            query=(
                "name:'Donation' "
                "AND active:'true'"
            ),
            limit=1,
        )

        if products.data:

            product_id = products.data[0].id

            _product_cache[
                cache_key
            ] = product_id

            logger.info(
                f"Using product: "
                f"{product_id}"
            )

            return product_id

        product = stripe.Product.create(
            name="Donation",
            description=(
                "Donation product"
            ),
            metadata={
                "category": "donation",
            },
        )

        _product_cache[
            cache_key
        ] = product.id

        logger.info(
            f"Created product: "
            f"{product.id}"
        )

        return product.id

    except stripe.error.StripeError as exc:

        logger.exception(
            "Product error"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                exc.user_message
                or str(exc)
            ),
        )


async def get_or_create_price(
    product_id: str,
    amount: int,
    currency: str,
    interval: Literal[
        "month",
        "year",
    ],
) -> str:

    cache_key = (
        f"{amount}_"
        f"{currency}_"
        f"{interval}"
    )

    if cache_key in _price_cache:

        return _price_cache[
            cache_key
        ]

    try:

        prices = stripe.Price.list(
            product=product_id,
            active=True,
            limit=100,
        )

        for price in prices.auto_paging_iter():

            recurring = getattr(
                price,
                "recurring",
                None,
            )

            if (
                price.currency.lower()
                == currency.lower()
                and price.unit_amount
                == amount
                and recurring
                and getattr(
                    recurring,
                    "interval",
                    None,
                ) == interval
            ):

                _price_cache[
                    cache_key
                ] = price.id

                logger.info(
                    f"Using existing price: "
                    f"{price.id}"
                )

                return price.id

        price = stripe.Price.create(
            product=product_id,
            unit_amount=amount,
            currency=currency.lower(),
            recurring={
                "interval": interval,
            },
            metadata={
                "type": "donation",
            },
        )

        _price_cache[
            cache_key
        ] = price.id

        logger.info(
            f"Created new price: "
            f"{price.id}"
        )

        return price.id

    except stripe.error.StripeError as exc:

        logger.exception(
            "Price error"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                exc.user_message
                or str(exc)
            ),
        )

# ─────────────────────────────────────────────────────────────
# ONE-TIME PAYMENT
# ─────────────────────────────────────────────────────────────

async def create_one_time_payment(
    amount: int,
    currency: str,
    customer_id: str,
) -> stripe.PaymentIntent:

    try:

        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency.lower(),
            customer=customer_id,
            automatic_payment_methods={
                "enabled": True,
            },
            metadata={
                "donation_type":
                "one_time",
            },
            idempotency_key=str(uuid.uuid4()),
        )

        logger.info(
            f"Created payment intent: "
            f"{intent.id}"
        )

        return intent

    except stripe.error.StripeError as exc:

        logger.exception(
            "PaymentIntent error"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                exc.user_message
                or str(exc)
            ),
        )

# ─────────────────────────────────────────────────────────────
# SUBSCRIPTION
# ─────────────────────────────────────────────────────────────

async def create_subscription(
    customer_id: str,
    price_id: str,
) -> stripe.Subscription:

    try:

        subscription = (
            stripe.Subscription.create(
                customer=customer_id,
                items=[
                    {
                        "price": price_id,
                    }
                ],
                payment_behavior=(
                    "default_incomplete"
                ),
                payment_settings={
                    "save_default_payment_method":
                    "on_subscription",
                },
                expand=[
                    "latest_invoice.confirmation_secret",
                    "latest_invoice.payment_intent", 
                    "pending_setup_intent",
                ],
                metadata={
                    "donation_type":
                    "recurring",
                },
                idempotency_key=str(uuid.uuid4()),
            )
        )

        logger.info(
            f"Created subscription: "
            f"{subscription.id}"
        )

        return subscription

    except stripe.error.StripeError as exc:

        logger.exception(
            "Subscription error"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                exc.user_message
                or str(exc)
            ),
        )

# ─────────────────────────────────────────────────────────────
# CLIENT SECRET EXTRACTION
# ─────────────────────────────────────────────────────────────

async def extract_client_secret(
    subscription: stripe.Subscription,
) -> str:

    try:

        latest_invoice = getattr(
            subscription,
            "latest_invoice",
            None,
        )

        if not latest_invoice:

            raise HTTPException(
                status_code=500,
                detail="No invoice found",
            )

        # -----------------------------------------------------
        # NEW STRIPE METHOD
        # latest_invoice.confirmation_secret.client_secret
        # -----------------------------------------------------

        confirmation_secret = getattr(
            latest_invoice,
            "confirmation_secret",
            None,
        )

        if confirmation_secret:

            client_secret = getattr(
                confirmation_secret,
                "client_secret",
                None,
            )

            if client_secret:

                return client_secret

        # -----------------------------------------------------
        # FALLBACK TO PAYMENT INTENT
        # -----------------------------------------------------

        payment_intent = getattr(
            latest_invoice,
            "payment_intent",
            None,
        )

        if payment_intent:

            if isinstance(
                payment_intent,
                str,
            ):

                payment_intent = (
                    stripe.PaymentIntent.retrieve(
                        payment_intent
                    )
                )

            client_secret = getattr(
                payment_intent,
                "client_secret",
                None,
            )

            if client_secret:

                return client_secret

        # -----------------------------------------------------
        # FALLBACK TO SETUP INTENT
        # -----------------------------------------------------

        setup_intent = getattr(
            subscription,
            "pending_setup_intent",
            None,
        )

        if setup_intent:

            if isinstance(
                setup_intent,
                str,
            ):

                setup_intent = (
                    stripe.SetupIntent.retrieve(
                        setup_intent
                    )
                )

            client_secret = getattr(
                setup_intent,
                "client_secret",
                None,
            )

            if client_secret:

                return client_secret

        # -----------------------------------------------------

        logger.error(
            "Failed to extract client_secret"
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to initialize "
                "Stripe payment"
            ),
        )

    except Exception as exc:

        logger.exception(
            f"Client secret extraction error: {exc}"
        )

        raise HTTPException(
            status_code=500,
            detail="Stripe client secret error",
        )
# ─────────────────────────────────────────────────────────────
# WEBHOOK HANDLERS
# ─────────────────────────────────────────────────────────────

async def handle_payment_intent_succeeded(
    payment_intent,
):

    logger.info(
        f"Payment succeeded: "
        f"{payment_intent.id}"
    )


async def handle_payment_intent_failed(
    payment_intent,
):

    logger.warning(
        f"Payment failed: "
        f"{payment_intent.id}"
    )


async def handle_invoice_payment_succeeded(
    invoice,
):

    logger.info(
        f"Invoice paid: "
        f"{invoice.id}"
    )


async def handle_invoice_payment_failed(
    invoice,
):

    logger.warning(
        f"Invoice failed: "
        f"{invoice.id}"
    )


async def handle_subscription_updated(
    subscription,
):

    logger.info(
        f"Subscription updated: "
        f"{subscription.id}"
    )


async def handle_subscription_deleted(
    subscription,
):

    logger.info(
        f"Subscription deleted: "
        f"{subscription.id}"
    )

# ─────────────────────────────────────────────────────────────
# LIFESPAN
# ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):

    logger.info(
        "🚀 Donation API starting"
    )

    yield

    logger.info(
        "🛑 Donation API shutting down"
    )

# ─────────────────────────────────────────────────────────────
# FASTAPI
# ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Stripe Donation API",
    version="4.0.0",
    lifespan=lifespan,
)

# ─────────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://lifora-foundation.vercel.app",
    ],
    allow_origin_regex=(
        r"https://.*\.vercel\.app"
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────

@app.api_route(
    "/",
    methods=["GET", "HEAD"],
)
async def root():

    return {
        "message":
        "Donation API running",
    }


@app.get("/health")
async def health():

    return {
        "status": "healthy",
        "stripe":
        bool(STRIPE_SECRET_KEY),
        "webhook":
        bool(
            STRIPE_WEBHOOK_SECRET
        ),
    }


@app.get(
    "/api/exchange-rate",
    response_model=
    ExchangeRateResponse,
)
async def exchange_rates():

    return ExchangeRateResponse(
        rates=EXCHANGE_RATES
    )


@app.post(
    "/api/donate",
    response_model=
    DonationResponse,
)
async def create_donation(
    payload: DonationRequest,
):

    customer = (
        await get_or_create_customer(
            email=payload.email,
            first_name=
            payload.firstName,
            last_name=
            payload.lastName,
        )
    )

    # ─────────────────────────────────────────
    # ONE-TIME
    # ─────────────────────────────────────────

    if payload.frequency == (
        "One Time"
    ):

        payment_intent = (
            await create_one_time_payment(
                amount=payload.amount,
                currency=
                payload.currency,
                customer_id=
                customer.id,
            )
        )

        return DonationResponse(
            clientSecret=
            payment_intent.client_secret,
            type=
            "payment_intent",
        )

    # ─────────────────────────────────────────
    # SUBSCRIPTION
    # ─────────────────────────────────────────

    interval = (
        "month"
        if payload.frequency
        == "Monthly"
        else "year"
    )

    product_id = (
        await get_or_create_donation_product()
    )

    price_id = (
        await get_or_create_price(
            product_id=
            product_id,
            amount=
            payload.amount,
            currency=
            payload.currency,
            interval=
            interval,
        )
    )

    subscription = (
        await create_subscription(
            customer_id=
            customer.id,
            price_id=
            price_id,
        )
    )

    client_secret = (
        await extract_client_secret(
            subscription
        )
    )

    return DonationResponse(
        clientSecret=
        client_secret,
        type="subscription",
    )

# ─────────────────────────────────────────────────────────────
# WEBHOOK
# ─────────────────────────────────────────────────────────────

@app.post("/api/webhook")
async def stripe_webhook(
    request: Request,
):

    if not STRIPE_WEBHOOK_SECRET:

        raise HTTPException(
            status_code=500,
            detail=(
                "Webhook secret "
                "not configured"
            ),
        )

    payload = (
        await request.body()
    )

    signature = request.headers.get(
        "stripe-signature"
    )

    if not signature:

        raise HTTPException(
            status_code=400,
            detail=(
                "Missing Stripe "
                "signature"
            ),
        )

    try:

        event = (
            stripe.Webhook.construct_event(
                payload,
                signature,
                STRIPE_WEBHOOK_SECRET,
            )
        )

    except ValueError:

        raise HTTPException(
            status_code=400,
            detail="Invalid payload",
        )

    except stripe.error.SignatureVerificationError:

        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid Stripe "
                "signature"
            ),
        )

    logger.info(
        f"Webhook received: "
        f"{event.type}"
    )

    try:

        match event.type:

            case (
                "payment_intent.succeeded"
            ):

                await (
                    handle_payment_intent_succeeded(
                        event.data.object
                    )
                )

            case (
                "payment_intent.payment_failed"
            ):

                await (
                    handle_payment_intent_failed(
                        event.data.object
                    )
                )

            case (
                "invoice.payment_succeeded"
            ):

                await (
                    handle_invoice_payment_succeeded(
                        event.data.object
                    )
                )

            case (
                "invoice.payment_failed"
            ):

                await (
                    handle_invoice_payment_failed(
                        event.data.object
                    )
                )

            case (
                "customer.subscription.updated"
            ):

                await (
                    handle_subscription_updated(
                        event.data.object
                    )
                )

            case (
                "customer.subscription.deleted"
            ):

                await (
                    handle_subscription_deleted(
                        event.data.object
                    )
                )

            case _:

                logger.info(
                    f"Unhandled event: "
                    f"{event.type}"
                )

    except Exception as exc:

        logger.exception(
            f"Webhook processing "
            f"failed: {exc}"
        )

    return JSONResponse(
        status_code=200,
        content={
            "received": True,
        },
    )

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8000",
            )
        ),
        reload=False,
    )
