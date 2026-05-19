"""
Production-ready FastAPI backend for Stripe donations.
Supports one-time payments and recurring subscriptions (Monthly/Yearly).
"""

import os
import logging
from contextlib import asynccontextmanager
from typing import Optional, Literal

import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, EmailStr, field_validator

# ─────────────────────────────────────────────────────────────
# Configuration & Logging
# ─────────────────────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("donation_api")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

if not STRIPE_SECRET_KEY:
    logger.warning("STRIPE_SECRET_KEY is not set. Stripe operations will fail.")
if not STRIPE_WEBHOOK_SECRET:
    logger.warning("STRIPE_WEBHOOK_SECRET is not set. Webhook verification will fail.")

stripe.api_key = STRIPE_SECRET_KEY

# Hardcoded exchange rates relative to CAD
EXCHANGE_RATES = {
    "USD": 1.35,
    "EUR": 1.45,
    "GBP": 1.70,
    "CAD": 1.00,
}

# In-memory cache for Stripe Product ID to avoid repeated lookups
_product_cache: dict[str, str] = {}


# ─────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────

class ExchangeRateResponse(BaseModel):
    rates: dict[str, float]


class DonationRequest(BaseModel):
    amount: int = Field(..., ge=100, description="Amount in smallest currency unit (cents)")
    frequency: Literal["One Time", "Monthly", "Yearly"]
    currency: str = Field(..., min_length=3, max_length=3, description="ISO 4217 currency code")
    firstName: str = Field(..., min_length=1, max_length=100)
    lastName: str = Field(..., min_length=1, max_length=100)
    email: EmailStr

    @field_validator("currency")
    @classmethod
    def currency_uppercase(cls, v: str) -> str:
        return v.upper()


class DonationResponse(BaseModel):
    clientSecret: str
    type: Literal["payment_intent", "subscription"]


# ─────────────────────────────────────────────────────────────
# Stripe Helpers
# ─────────────────────────────────────────────────────────────

async def get_or_create_customer(email: str, first_name: str, last_name: str) -> stripe.Customer:
    """
    Search for an existing Stripe Customer by email.
    Create one if not found.
    """
    try:
        # Search for existing customer by email
        customers = stripe.Customer.search(
            query=f"email:'{email}'",
            limit=1,
        )
        if customers.data:
            customer = customers.data[0]
            logger.info(f"Found existing customer: {customer.id}")
            return customer

        # Create new customer
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
        logger.error(f"Stripe customer error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stripe customer error: {exc.user_message or str(exc)}",
        )


async def get_or_create_donation_product() -> str:
    """
    Find or create a Stripe Product named 'Donation'.
    Uses in-memory caching.
    """
    cache_key = "donation_product"
    if cache_key in _product_cache:
        return _product_cache[cache_key]

    try:
        # Search for existing product
        products = stripe.Product.search(
            query="name:'Donation' AND active:'true'",
            limit=1,
        )
        if products.data:
            product_id = products.data[0].id
            _product_cache[cache_key] = product_id
            logger.info(f"Found existing donation product: {product_id}")
            return product_id

        # Create new product
        product = stripe.Product.create(
            name="Donation",
            description="Recurring donation to support our cause",
            metadata={"category": "donation"},
        )
        _product_cache[cache_key] = product.id
        logger.info(f"Created donation product: {product.id}")
        return product.id

    except stripe.error.StripeError as exc:
        logger.error(f"Stripe product error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stripe product error: {exc.user_message or str(exc)}",
        )


async def create_recurring_price(
    product_id: str,
    amount: int,
    currency: str,
    interval: Literal["month", "year"],
) -> stripe.Price:
    """
    Create a recurring Stripe Price for the given product.
    """
    try:
        price = stripe.Price.create(
            product=product_id,
            unit_amount=amount,
            currency=currency.lower(),
            recurring={"interval": interval},
            metadata={"type": "donation"},
        )
        logger.info(f"Created recurring price: {price.id}")
        return price

    except stripe.error.StripeError as exc:
        logger.error(f"Stripe price error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stripe price error: {exc.user_message or str(exc)}",
        )


async def create_one_time_payment(
    amount: int,
    currency: str,
    customer_id: str,
) -> stripe.PaymentIntent:
    """
    Create a PaymentIntent for a one-time donation.
    """
    try:
        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency.lower(),
            customer=customer_id,
            setup_future_usage="off_session",  # Save card for future use
            metadata={
                "donation_type": "one_time",
                "customer_id": customer_id,
            },
            automatic_payment_methods={"enabled": True},
        )
        logger.info(f"Created PaymentIntent: {intent.id}")
        return intent

    except stripe.error.StripeError as exc:
        logger.error(f"Stripe PaymentIntent error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stripe PaymentIntent error: {exc.user_message or str(exc)}",
        )


async def create_subscription(
    customer_id: str,
    price_id: str,
) -> stripe.Subscription:
    """
    Create a subscription with default_incomplete payment behavior.
    Expands latest_invoice.payment_intent to get the client secret.
    """
    try:
        subscription = stripe.Subscription.create(
            customer=customer_id,
            items=[{"price": price_id}],
            payment_behavior="default_incomplete",
            expand=["latest_invoice.payment_intent"],
            metadata={"donation_type": "recurring"},
        )
        logger.info(f"Created Subscription: {subscription.id}")
        return subscription

    except stripe.error.StripeError as exc:
        logger.error(f"Stripe Subscription error: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stripe Subscription error: {exc.user_message or str(exc)}",
        )


# ─────────────────────────────────────────────────────────────
# Webhook Handlers
# ─────────────────────────────────────────────────────────────

async def handle_payment_intent_succeeded(payment_intent: stripe.PaymentIntent) -> None:
    """Handle successful payment intent."""
    logger.info(
        f"[payment_intent.succeeded] ID: {payment_intent.id}, "
        f"Amount: {payment_intent.amount}, "
        f"Customer: {payment_intent.customer}"
    )
    # TODO: Update database - mark donation as completed
    # Example: await db.donations.update_one(
    #     {"payment_intent_id": payment_intent.id},
    #     {"$set": {"status": "succeeded", "paid_at": datetime.utcnow()}}
    # )


async def handle_payment_intent_failed(payment_intent: stripe.PaymentIntent) -> None:
    """Handle failed payment intent."""
    logger.warning(
        f"[payment_intent.payment_failed] ID: {payment_intent.id}, "
        f"Error: {payment_intent.last_payment_error}"
    )
    # TODO: Update database - mark donation as failed, notify user


async def handle_invoice_payment_succeeded(invoice: stripe.Invoice) -> None:
    """Handle successful invoice payment (crucial for subscriptions)."""
    logger.info(
        f"[invoice.payment_succeeded] Invoice: {invoice.id}, "
        f"Subscription: {invoice.subscription}, "
        f"Amount Paid: {invoice.amount_paid}"
    )
    # TODO: Update database - record subscription payment
    # Example: await db.subscription_payments.insert_one({...})


async def handle_invoice_payment_failed(invoice: stripe.Invoice) -> None:
    """Handle failed invoice payment."""
    logger.warning(
        f"[invoice.payment_failed] Invoice: {invoice.id}, "
        f"Subscription: {invoice.subscription}, "
        f"Attempt Count: {invoice.attempt_count}"
    )
    # TODO: Update database - mark payment as failed, trigger dunning flow


async def handle_subscription_updated(subscription: stripe.Subscription) -> None:
    """Handle subscription updates."""
    logger.info(
        f"[customer.subscription.updated] ID: {subscription.id}, "
        f"Status: {subscription.status}, "
        f"Cancel At Period End: {subscription.cancel_at_period_end}"
    )
    # TODO: Update database - sync subscription status


async def handle_subscription_deleted(subscription: stripe.Subscription) -> None:
    """Handle subscription deletion/cancellation."""
    logger.info(
        f"[customer.subscription.deleted] ID: {subscription.id}, "
        f"Status: {subscription.status}"
    )
    # TODO: Update database - mark subscription as cancelled


# ─────────────────────────────────────────────────────────────
# FastAPI Application
# ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("🚀 Donation API starting up...")
    yield
    logger.info("🛑 Donation API shutting down...")


app = FastAPI(
    title="Stripe Donation API",
    description="Production-ready FastAPI backend for one-time and recurring Stripe donations.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Restrict to your frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────
# API Endpoints
# ─────────────────────────────────────────────────────────────

@app.get("/api/exchange-rate", response_model=ExchangeRateResponse)
async def get_exchange_rates() -> ExchangeRateResponse:
    """
    Return hardcoded exchange rates relative to CAD.
    """
    return ExchangeRateResponse(rates=EXCHANGE_RATES)


@app.post("/api/donate", response_model=DonationResponse)
async def create_donation(payload: DonationRequest) -> DonationResponse:
    """
    Create a donation (one-time or recurring).
    
    Steps:
    1. Find or create Stripe Customer by email.
    2. Create PaymentIntent (one-time) or Subscription (recurring).
    3. Return client secret for frontend confirmation.
    """
    # Step 1: Get or create customer
    customer = await get_or_create_customer(
        email=payload.email,
        first_name=payload.firstName,
        last_name=payload.lastName,
    )

    # Step 2: Branch based on frequency
    if payload.frequency == "One Time":
        intent = await create_one_time_payment(
            amount=payload.amount,
            currency=payload.currency,
            customer_id=customer.id,
        )
        return DonationResponse(
            clientSecret=intent.client_secret,
            type="payment_intent",
        )

    # Recurring: Monthly or Yearly
    interval = "month" if payload.frequency == "Monthly" else "year"

    # 2a. Find or create Donation product
    product_id = await get_or_create_donation_product()

    # 2b. Create recurring price
    price = await create_recurring_price(
        product_id=product_id,
        amount=payload.amount,
        currency=payload.currency,
        interval=interval,
    )

    # 2c. Create subscription
    subscription = await create_subscription(
        customer_id=customer.id,
        price_id=price.id,
    )

    # Extract client secret from expanded latest_invoice.payment_intent
    latest_invoice = subscription.latest_invoice
    if not latest_invoice or not latest_invoice.payment_intent:
        logger.error("Subscription created but no payment_intent found in latest_invoice")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve payment intent from subscription invoice.",
        )

    return DonationResponse(
        clientSecret=latest_invoice.payment_intent.client_secret,
        type="subscription",
    )


@app.post("/api/webhook")
async def stripe_webhook(request: Request) -> JSONResponse:
    """
    Handle Stripe webhooks with signature verification.
    
    CRITICAL: Must read raw request body for signature verification.
    Do NOT let FastAPI parse JSON automatically.
    """
    # Read raw body
    payload_bytes = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        logger.warning("Webhook received without Stripe-Signature header")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe-Signature header",
        )

    # Verify signature
    try:
        event = stripe.Webhook.construct_event(
            payload_bytes,
            sig_header,
            STRIPE_WEBHOOK_SECRET,
        )
    except ValueError as exc:
        # Invalid payload
        logger.error(f"Invalid webhook payload: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid payload",
        )
    except stripe.error.SignatureVerificationError as exc:
        # Invalid signature
        logger.error(f"Invalid webhook signature: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature",
        )

    logger.info(f"Webhook received: {event.type} | ID: {event.id}")

    # Route to appropriate handler
    try:
        match event.type:
            case "payment_intent.succeeded":
                await handle_payment_intent_succeeded(event.data.object)

            case "payment_intent.payment_failed":
                await handle_payment_intent_failed(event.data.object)

            case "invoice.payment_succeeded":
                await handle_invoice_payment_succeeded(event.data.object)

            case "invoice.payment_failed":
                await handle_invoice_payment_failed(event.data.object)

            case "customer.subscription.updated":
                await handle_subscription_updated(event.data.object)

            case "customer.subscription.deleted":
                await handle_subscription_deleted(event.data.object)

            case _:
                logger.info(f"Unhandled webhook event type: {event.type}")

    except Exception as exc:
        logger.exception(f"Error processing webhook {event.type}: {exc}")
        # Return 200 to Stripe so it doesn't retry indefinitely for non-retryable errors
        # For transient errors, you might want to return 500 to trigger retry
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "error", "message": "Event received but processing failed"},
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "success", "event_type": event.type},
    )


# ─────────────────────────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "stripe_configured": bool(STRIPE_SECRET_KEY),
        "webhook_configured": bool(STRIPE_WEBHOOK_SECRET),
    }


# ─────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("ENV", "production").lower() == "development",
    )
