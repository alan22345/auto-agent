"""Tests for the pipeline — must still pass after optimization."""

import csv
import os
import tempfile

from pipeline import (
    SaleRecord,
    aggregate_by_category,
    clean_records,
    load_records,
    monthly_trends,
    run_pipeline,
    top_salespersons,
)


def _create_test_csv(rows: list[dict]) -> str:
    """Create a temporary CSV file with the given rows."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "date", "product", "category", "quantity", "unit_price", "region", "salesperson"
        ])
        writer.writeheader()
        writer.writerows(rows)
    return path


SAMPLE_ROWS = [
    {"date": "2024-01-15", "product": "Widget A", "category": "electronics",
     "quantity": "10", "unit_price": "29.99", "region": "north", "salesperson": "Alice"},
    {"date": "2024-01-20", "product": "Widget B", "category": "electronics",
     "quantity": "5", "unit_price": "49.99", "region": "south", "salesperson": "Bob"},
    {"date": "2024-02-10", "product": "Gadget X", "category": "accessories",
     "quantity": "20", "unit_price": "9.99", "region": "North", "salesperson": "Alice"},
    {"date": "2024-02-15", "product": "Widget A", "category": "Electronics",
     "quantity": "3", "unit_price": "29.99", "region": "east", "salesperson": "Carol"},
    {"date": "invalid-date", "product": "Bad", "category": "x",
     "quantity": "1", "unit_price": "1.0", "region": "west", "salesperson": "Dave"},
    {"date": "2024-03-01", "product": "Gadget Y", "category": "accessories",
     "quantity": "0", "unit_price": "15.00", "region": "south", "salesperson": "Bob"},
]


def test_load_records():
    path = _create_test_csv(SAMPLE_ROWS)
    records = load_records(path)
    assert len(records) == 6
    os.unlink(path)


def test_clean_records():
    path = _create_test_csv(SAMPLE_ROWS)
    records = load_records(path)
    cleaned = clean_records(records)
    # Should remove: invalid date (1), zero quantity (1) = 4 remaining
    assert len(cleaned) == 4
    # Regions should be title-cased
    regions = [r.region for r in cleaned]
    assert all(r[0].isupper() for r in regions)
    os.unlink(path)


def test_aggregate_by_category():
    records = [
        SaleRecord("2024-01-01", "A", "electronics", 10, 29.99, "North", "Alice"),
        SaleRecord("2024-01-02", "B", "electronics", 5, 49.99, "South", "Bob"),
        SaleRecord("2024-01-03", "C", "accessories", 20, 9.99, "East", "Carol"),
    ]
    result = aggregate_by_category(records)
    assert "electronics" in result
    assert "accessories" in result
    assert result["electronics"]["total_quantity"] == 15
    assert result["accessories"]["total_quantity"] == 20


def test_top_salespersons():
    records = [
        SaleRecord("2024-01-01", "A", "x", 10, 100.0, "N", "Alice"),
        SaleRecord("2024-01-02", "B", "x", 5, 50.0, "S", "Bob"),
        SaleRecord("2024-01-03", "C", "x", 1, 10.0, "E", "Carol"),
    ]
    top = top_salespersons(records, n=2)
    assert len(top) == 2
    assert top[0]["salesperson"] == "Alice"
    assert top[0]["total_revenue"] == 1000.0


def test_monthly_trends():
    records = [
        SaleRecord("2024-01-15", "A", "x", 10, 10.0, "N", "Alice"),
        SaleRecord("2024-01-20", "B", "x", 5, 20.0, "S", "Bob"),
        SaleRecord("2024-02-10", "C", "x", 3, 30.0, "E", "Carol"),
    ]
    trends = monthly_trends(records)
    assert "2024-01" in trends
    assert "2024-02" in trends
    assert trends["2024-01"]["count"] == 2
    assert trends["2024-02"]["revenue"] == 90.0


def test_full_pipeline():
    path = _create_test_csv(SAMPLE_ROWS)
    result = run_pipeline(path)
    assert result["total_records"] == 6
    assert result["valid_records"] == 4
    assert "by_category" in result
    assert "top_salespersons" in result
    assert "monthly_trends" in result
    os.unlink(path)
