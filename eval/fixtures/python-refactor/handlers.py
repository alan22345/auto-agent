"""Request handlers with duplicated validation logic."""

import re
from datetime import datetime


def handle_create_user(data: dict) -> dict:
    """Create a new user."""
    # Validate email
    email = data.get("email", "")
    if not email or not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
        return {"error": "Invalid email address"}

    # Validate name
    name = data.get("name", "")
    if not name or len(name) < 2 or len(name) > 100:
        return {"error": "Name must be between 2 and 100 characters"}

    # Validate age
    age = data.get("age")
    if age is not None:
        try:
            age = int(age)
            if age < 0 or age > 150:
                return {"error": "Age must be between 0 and 150"}
        except (ValueError, TypeError):
            return {"error": "Age must be a number"}

    return {"status": "created", "user": {"name": name, "email": email, "age": age}}


def handle_update_user(data: dict) -> dict:
    """Update an existing user."""
    # Validate email
    email = data.get("email", "")
    if not email or not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
        return {"error": "Invalid email address"}

    # Validate name
    name = data.get("name", "")
    if not name or len(name) < 2 or len(name) > 100:
        return {"error": "Name must be between 2 and 100 characters"}

    # Validate age
    age = data.get("age")
    if age is not None:
        try:
            age = int(age)
            if age < 0 or age > 150:
                return {"error": "Age must be between 0 and 150"}
        except (ValueError, TypeError):
            return {"error": "Age must be a number"}

    user_id = data.get("id")
    if not user_id:
        return {"error": "User ID is required"}

    return {"status": "updated", "user": {"id": user_id, "name": name, "email": email, "age": age}}


def handle_create_order(data: dict) -> dict:
    """Create a new order."""
    # Validate email
    email = data.get("customer_email", "")
    if not email or not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', email):
        return {"error": "Invalid customer email address"}

    # Validate amount
    amount = data.get("amount")
    if amount is None:
        return {"error": "Amount is required"}
    try:
        amount = float(amount)
        if amount <= 0:
            return {"error": "Amount must be positive"}
    except (ValueError, TypeError):
        return {"error": "Amount must be a number"}

    return {
        "status": "created",
        "order": {
            "customer_email": email,
            "amount": amount,
            "created_at": datetime.utcnow().isoformat(),
        },
    }
