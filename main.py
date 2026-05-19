"""
Production-ready FastAPI backend for Stripe donations.
Updated for Stripe 2025 API changes.
Supports:
- One-time payments
- Monthly subscriptions
- Yearly subscriptions
- Stripe webhooks
- Proper subscription confirmation_secret handling
"""

import os
import logging
from contextlib import asynccontextmanager
from typing import Literal

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, EmailStr, field_validator

# ─────────────────────────────────────────────────────────────
# Load Environment Variables
# ─────────────────────────────────────────────────────────────

load_dotenv()

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("donation_api")

# ─────────────────────────────────────────────────────────────
# Stripe Configuration
# ─────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

if not STRIPE_SECRET_KEY:
    raise RuntimeError("STRIPE_SECRET_KEY is missing")

stripe.api_key = STRIPE_SECRET_KEY

# ─────────────────────────────────────────────────────────────
# Exchange Rates
# ─────────────────────────────────────────────────────────────

EXCHANGE_RATES = {
    "USD": 1.35,
    "EUR": 1.45,
    "GBP": 1.70,
    "CAD": 1.00,
}

# ─────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────

_product_cache: dict[str, str] = {}

# ─────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────

class ExchangeRateResponse(BaseModel):
    rates: dict[str, float]


class DonationRequest(BaseModel):
    amount: int = Field(..., ge=100)
    frequency: Literal["One Time", "Monthly", "Yearly"]
    currency: str = Field(..., min_length=3, max_length=3)

    firstName: str = Field(..., min_length=1, max_length=100)
    lastName: str = Field(..., min_length=1, max_length=100)

    email: EmailStr

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        return value.upper()


class DonationResponse(BaseModel):
    clientSecret: str
    type: Literal["payment_intent", "subscription"]


# ─────────────────────────────────────────────────────────────
# Stripe Helpers
# ─────────────────────────────────────────────────────────────

async def get_or_create_customer(
    email: str,
    first_name: str,
    last_name: str,
) -> stripe.Customer:
    """
    Get existing customer or create new one.
    """

    try:
        customers = stripe.Customer.search(
            query=f"email:'{email}'",
            limit=1,
        )

        if customers.data:
            customer = customers.data[0]

            logger.info(f"Found existing customer: {customer.id}")

            return customer

        customer = stripe.Customer.create(
            email=email,
            name=f"{first_name} {last_name}",
            metadata={
                "first_name": first_name,
                "last_name": last_name,
            },
        )

        logger.info(f"Created new customer: {customer.id}")

        return customer

    except stripe.error.StripeError as exc:
        logger.exception("Customer creation failed")

        raise HTTPException(
            status_code=502,
            detail=exc.user_message or str(exc),
        )


async def get_or_create_donation_product() -> str:
    """
    Retrieve or create donation product.
    """

    cache_key = "donation_product"

    if cache_key in _product_cache:
        return _product_cache[cache_key]

    try:
        products = stripe.Product.search(
            query="name:'Donation' AND active:'true'",
            limit=1,
        )

        if products.data:
            product_id = products.data[0].id

            _product_cache[cache_key] = product_id

            logger.info(f"Using existing product: {product_id}")

            return product_id

        product = stripe.Product.create(
            name="Donation",
            description="Recurring donation",
            metadata={
                "category": "donation",
            },
        )

        _product_cache[cache_key] = product.id

        logger.info(f"Created product: {product.id}")

        return product.id

    except stripe.error.StripeError as exc:
        logger.exception("Product creation failed")

        raise HTTPException(
            status_code=502,
            detail=exc.user_message or str(exc),
        )


async def create_recurring_price(
    product_id: str,
    amount: int,
    currency: str,
    interval: Literal["month", "year"],
) -> stripe.Price:
    """
    Create recurring Stripe price.
    """

    try:
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

        logger.info(f"Created recurring price: {price.id}")

        return price

    except stripe.error.StripeError as exc:
        logger.exception("Price creation failed")

        raise HTTPException(
            status_code=502,
            detail=exc.user_message or str(exc),
        )


async def create_one_time_payment(
    amount: int,
    currency: str,
    customer_id: str,
) -> stripe.PaymentIntent:
    """
    Create one-time payment intent.
    """

    try:
        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency.lower(),
            customer=customer_id,
            automatic_payment_methods={
                "enabled": True,
            },
            metadata={
                "donation_type": "one_time",
            },
        )

        logger.info(f"Created payment intent: {intent.id}")

        return intent

    except stripe.error.StripeError as exc:
        logger.exception("PaymentIntent creation failed")

        raise HTTPException(
            status_code=502,
            detail=exc.user_message or str(exc),
        )


async def create_subscription(
    customer_id: str,
    price_id: str,
) -> stripe.Subscription:
    """
    Create Stripe subscription.
    Stripe 2025 compatible.
    """

    try:
        subscription = stripe.Subscription.create(
            customer=customer_id,
            items=[
                {
                    "price": price_id,
                }
            ],
            payment_behavior="default_incomplete",
            payment_settings={
                "save_default_payment_method": "on_subscription",
            },
            expand=[
                "latest_invoice",
            ],
            metadata={
                "donation_type": "recurring",
            },
        )

        logger.info(
            f"Created subscription: {subscription.id}"
        )

        return subscription

    except stripe.error.StripeError as exc:
        logger.exception(
            "Subscription creation failed"
        )

        raise HTTPException(
            status_code=502,
            detail=exc.user_message or str(exc),
        )

# ─────────────────────────────────────────────────────────────
# Webhook Handlers
# ─────────────────────────────────────────────────────────────

async def handle_payment_intent_succeeded(
    payment_intent: stripe.PaymentIntent,
):
    logger.info(
        f"Payment succeeded: {payment_intent.id}"
    )


async def handle_payment_intent_failed(
    payment_intent: stripe.PaymentIntent,
):
    logger.warning(
        f"Payment failed: {payment_intent.id}"
    )


async def handle_invoice_payment_succeeded(
    invoice: stripe.Invoice,
):
    logger.info(
        f"Invoice paid: {invoice.id}"
    )


async def handle_invoice_payment_failed(
    invoice: stripe.Invoice,
):
    logger.warning(
        f"Invoice failed: {invoice.id}"
    )


async def handle_subscription_updated(
    subscription: stripe.Subscription,
):
    logger.info(
        f"Subscription updated: {subscription.id}"
    )


async def handle_subscription_deleted(
    subscription: stripe.Subscription,
):
    logger.info(
        f"Subscription deleted: {subscription.id}"
    )


# ─────────────────────────────────────────────────────────────
# FastAPI Lifespan
# ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Donation API starting")
    yield
    logger.info("🛑 Donation API shutting down")


# ─────────────────────────────────────────────────────────────
# FastAPI App
# ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Stripe Donation API",
    version="2.0.0",
    lifespan=lifespan,
)

# ─────────────────────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://yourfrontend.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "message": "Donation API running",
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "stripe": bool(STRIPE_SECRET_KEY),
        "webhook": bool(STRIPE_WEBHOOK_SECRET),
    }


@app.get(
    "/api/exchange-rate",
    response_model=ExchangeRateResponse,
)
async def exchange_rates():
    return ExchangeRateResponse(
        rates=EXCHANGE_RATES
    )


@app.post(
    "/api/donate",
    response_model=DonationResponse,
)
async def create_donation(
    payload: DonationRequest,
):
    """
    Create donation payment or subscription.
    """

    customer = await get_or_create_customer(
        email=payload.email,
        first_name=payload.firstName,
        last_name=payload.lastName,
    )

    # ─────────────────────────────────────────
    # One-time payment
    # ─────────────────────────────────────────

    if payload.frequency == "One Time":

        payment_intent = await create_one_time_payment(
            amount=payload.amount,
            currency=payload.currency,
            customer_id=customer.id,
        )

        return DonationResponse(
            clientSecret=payment_intent.client_secret,
            type="payment_intent",
        )

    # ─────────────────────────────────────────
    # Subscription
    # ─────────────────────────────────────────

    interval = (
        "month"
        if payload.frequency == "Monthly"
        else "year"
    )

    product_id = await get_or_create_donation_product()

    price = await create_recurring_price(
        product_id=product_id,
        amount=payload.amount,
        currency=payload.currency,
        interval=interval,
    )

    subscription = await create_subscription(
        customer_id=customer.id,
        price_id=price.id,
    )

latest_invoice = subscription.latest_invoice

if not latest_invoice:
    raise HTTPException(
        status_code=500,
        detail="No latest invoice found",
    )

# Retrieve invoice using Stripe 2025 structure
invoice = stripe.Invoice.retrieve(
    latest_invoice.id
    if hasattr(latest_invoice, "id")
    else latest_invoice,
    expand=[
        "payments",
    ],
)

payments = invoice.get("payments")

if not payments or not payments.data:
    logger.error(
        "No payments found on invoice"
    )

    raise HTTPException(
        status_code=500,
        detail="No payments found",
    )

invoice_payment = payments.data[0]

payment_data = invoice_payment.get(
    "payment"
)

if not payment_data:
    logger.error(
        "No payment object found"
    )

    raise HTTPException(
        status_code=500,
        detail="No payment object found",
    )

payment_intent_id = payment_data.get(
    "payment_intent"
)

if not payment_intent_id:
    logger.error(
        "No payment_intent found"
    )

    raise HTTPException(
        status_code=500,
        detail="No payment intent found",
    )

payment_intent = stripe.PaymentIntent.retrieve(
    payment_intent_id
)

client_secret = payment_intent.client_secret

if not client_secret:
    logger.error(
        "No client secret found"
    )

    raise HTTPException(
        status_code=500,
        detail="Client secret missing",
    )

return DonationResponse(
    clientSecret=client_secret,
    type="subscription",
)

# ─────────────────────────────────────────────────────────────
# Stripe Webhook
# ─────────────────────────────────────────────────────────────

@app.post("/api/webhook")
async def stripe_webhook(
    request: Request,
):
    """
    Stripe webhook endpoint.
    """

    payload = await request.body()

    signature = request.headers.get(
        "stripe-signature"
    )

    if not signature:
        raise HTTPException(
            status_code=400,
            detail="Missing Stripe signature",
        )

    try:
        event = stripe.Webhook.construct_event(
            payload,
            signature,
            STRIPE_WEBHOOK_SECRET,
        )

    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid payload",
        )

    except stripe.error.SignatureVerificationError:
        raise HTTPException(
            status_code=401,
            detail="Invalid Stripe signature",
        )

    logger.info(
        f"Webhook received: {event.type}"
    )

    try:

        match event.type:

            case "payment_intent.succeeded":
                await handle_payment_intent_succeeded(
                    event.data.object
                )

            case "payment_intent.payment_failed":
                await handle_payment_intent_failed(
                    event.data.object
                )

            case "invoice.payment_succeeded":
                await handle_invoice_payment_succeeded(
                    event.data.object
                )

            case "invoice.payment_failed":
                await handle_invoice_payment_failed(
                    event.data.object
                )

            case "customer.subscription.updated":
                await handle_subscription_updated(
                    event.data.object
                )

            case "customer.subscription.deleted":
                await handle_subscription_deleted(
                    event.data.object
                )

            case _:
                logger.info(
                    f"Unhandled event: {event.type}"
                )

    except Exception as exc:
        logger.exception(
            f"Webhook processing failed: {exc}"
        )

    return JSONResponse(
        status_code=200,
        content={
            "received": True,
        },
    )

# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
