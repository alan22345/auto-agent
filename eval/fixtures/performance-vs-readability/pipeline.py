"""Data pipeline — reads CSV records, transforms, and aggregates.

This pipeline processes sales data. It works correctly but is slow on
large datasets (100k+ rows). The code is clean and readable, but
performance needs improvement.

Key constraint: the code must remain understandable to junior developers
who maintain it. Clever optimizations that sacrifice readability are not
acceptable. Find the right balance.
"""

import csv
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass
class SaleRecord:
    date: str
    product: str
    category: str
    quantity: int
    unit_price: float
    region: str
    salesperson: str


def load_records(filepath: str) -> list[SaleRecord]:
    """Load sales records from CSV."""
    records = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(SaleRecord(
                date=row["date"],
                product=row["product"],
                category=row["category"],
                quantity=int(row["quantity"]),
                unit_price=float(row["unit_price"]),
                region=row["region"],
                salesperson=row["salesperson"],
            ))
    return records


def clean_records(records: list[SaleRecord]) -> list[SaleRecord]:
    """Clean and validate records.

    PERFORMANCE ISSUE: This iterates the full list 3 times.
    """
    # Pass 1: Remove records with invalid dates
    valid_date = []
    for r in records:
        try:
            datetime.strptime(r.date, "%Y-%m-%d")
            valid_date.append(r)
        except ValueError:
            pass

    # Pass 2: Remove records with non-positive quantities
    valid_qty = []
    for r in valid_date:
        if r.quantity > 0 and r.unit_price > 0:
            valid_qty.append(r)

    # Pass 3: Normalize region names
    cleaned = []
    for r in valid_qty:
        r.region = r.region.strip().title()
        r.product = r.product.strip()
        r.category = r.category.strip().lower()
        cleaned.append(r)

    return cleaned


def aggregate_by_category(records: list[SaleRecord]) -> dict[str, dict]:
    """Aggregate sales metrics by category.

    PERFORMANCE ISSUE: Builds intermediate lists, then iterates again.
    """
    categories: dict[str, list[SaleRecord]] = {}
    for r in records:
        if r.category not in categories:
            categories[r.category] = []
        categories[r.category].append(r)

    result = {}
    for cat, cat_records in categories.items():
        total_revenue = 0
        total_quantity = 0
        for r in cat_records:
            total_revenue += r.quantity * r.unit_price
            total_quantity += r.quantity

        result[cat] = {
            "total_revenue": round(total_revenue, 2),
            "total_quantity": total_quantity,
            "record_count": len(cat_records),
            "avg_price": round(total_revenue / total_quantity, 2) if total_quantity else 0,
        }
    return result


def top_salespersons(records: list[SaleRecord], n: int = 10) -> list[dict]:
    """Find top N salespersons by total revenue.

    PERFORMANCE ISSUE: Sorts the full list when we only need top N.
    """
    revenue_by_person: dict[str, float] = {}
    for r in records:
        revenue = r.quantity * r.unit_price
        if r.salesperson in revenue_by_person:
            revenue_by_person[r.salesperson] += revenue
        else:
            revenue_by_person[r.salesperson] = revenue

    # Sort ALL entries just to get top N
    sorted_persons = sorted(
        revenue_by_person.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    return [
        {"salesperson": name, "total_revenue": round(rev, 2)}
        for name, rev in sorted_persons[:n]
    ]


def monthly_trends(records: list[SaleRecord]) -> dict[str, dict]:
    """Aggregate sales by month.

    PERFORMANCE ISSUE: Parses date string for every record.
    """
    months: dict[str, dict] = {}
    for r in records:
        # Parse date every time (expensive)
        dt = datetime.strptime(r.date, "%Y-%m-%d")
        month_key = dt.strftime("%Y-%m")

        if month_key not in months:
            months[month_key] = {"revenue": 0, "quantity": 0, "count": 0}

        months[month_key]["revenue"] += r.quantity * r.unit_price
        months[month_key]["quantity"] += r.quantity
        months[month_key]["count"] += 1

    # Round revenue
    for m in months.values():
        m["revenue"] = round(m["revenue"], 2)

    return months


def run_pipeline(filepath: str) -> dict:
    """Run the full pipeline and return all aggregations."""
    records = load_records(filepath)
    cleaned = clean_records(records)
    return {
        "total_records": len(records),
        "valid_records": len(cleaned),
        "by_category": aggregate_by_category(cleaned),
        "top_salespersons": top_salespersons(cleaned),
        "monthly_trends": monthly_trends(cleaned),
    }
