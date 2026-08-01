# LibreChat Artifact + ClickHouse Dashboard Architecture

## Overview

This document describes an architecture for building interactive analytics dashboards in a LibreChat artifact while keeping all database credentials secure.

The key design principle is:

* **The artifact is responsible for presentation and user interaction.**
* **The backend is responsible for data retrieval and database access.**

The artifact never communicates directly with ClickHouse and never has access to database credentials.

---

# Architecture

```
┌─────────────────────────────┐
│     LibreChat Artifact      │
│                             │
│ • Charts                    │
│ • Tables                    │
│ • Filters                   │
│ • Dashboard state           │
└──────────────┬──────────────┘
               │
        HTTPS API Calls
               │
┌──────────────▼──────────────┐
│      Backend Service        │
│                             │
│ • Reads .env                │
│ • Connects to ClickHouse    │
│ • Executes parameterized SQL│
│ • Returns JSON              │
└──────────────┬──────────────┘
               │
        ClickHouse Client
               │
┌──────────────▼──────────────┐
│        ClickHouse           │
└─────────────────────────────┘
```

---

# Responsibilities

## LibreChat Artifact

The artifact is purely responsible for the user experience.

It should manage:

* Dashboard layout
* Line charts
* Bar charts
* Tables
* KPI cards
* Filter controls
* Current filter state
* API requests
* Chart updates

The artifact should never:

* Read `.env`
* Store database credentials
* Execute SQL
* Connect directly to ClickHouse

---

## Backend

The backend is responsible for all database interaction.

Responsibilities include:

* Reading environment variables
* Authenticating with ClickHouse
* Building parameterized SQL queries
* Executing queries
* Returning JSON responses
* Validation of user inputs
* Authorization (if required)

---

# Dashboard State

Rather than each chart maintaining its own filters, the dashboard maintains a single shared filter state.

Example:

```javascript
const dashboardFilters = {
    from: "2026-01-01T00:00:00",
    to: "2026-01-31T23:59:59",
    stores: ["London", "Paris"],
    granularity: "day"
};
```

Every visualization uses this shared state.

Benefits:

* One consistent source of truth
* Easy synchronization across charts
* Simpler code
* Predictable behaviour

---

# Filter Components

Example dashboard controls:

* Date From
* Date To
* Store selector
* Region selector
* Product selector
* Granularity (Hour / Day / Week / Month)

Example layout:

```
+----------------------------------------------------+
| Date From | Date To | Store | Region | Granularity |
+----------------------------------------------------+

+---------------- Revenue by Store ------------------+

+---------------- Revenue by Product ----------------+

+---------------- KPI Cards -------------------------+

+---------------- Orders Table ----------------------+
```

---

# Filter Flow

## 1. User changes filters

Example:

```
From:
2026-01-01

To:
2026-01-31

Store:
London
Paris

Granularity:
Day
```

Nothing is queried yet.

---

## 2. User clicks Apply

The artifact gathers all filter values into a single object.

```
{
    from,
    to,
    stores,
    granularity
}
```

---

## 3. Artifact calls backend

Example request:

```
POST /api/revenue
```

Body:

```json
{
    "from":"2026-01-01",
    "to":"2026-01-31",
    "stores":["London","Paris"],
    "granularity":"day"
}
```

---

## 4. Backend builds SQL

Example:

```sql
SELECT
    toStartOfDay(timestamp) AS bucket,
    store,
    SUM(revenue) AS revenue
FROM sales
WHERE timestamp >= {from:DateTime}
  AND timestamp <= {to:DateTime}
  AND store IN {stores:Array(String)}
GROUP BY
    bucket,
    store
ORDER BY bucket;
```

The backend substitutes parameters safely using the ClickHouse client.

---

## 5. Backend returns JSON

Example:

```json
[
    {
        "bucket":"2026-01-01",
        "store":"London",
        "revenue":1200
    },
    {
        "bucket":"2026-01-01",
        "store":"Paris",
        "revenue":980
    }
]
```

---

## 6. Artifact redraws charts

The artifact updates the existing visualization using the returned data.

No page refresh is required.

---

# Multiple Charts

A single filter state should drive every visualization.

```
Shared Filters
      │
      ▼
+--------------------+
| Dashboard State    |
+--------------------+
      │
      ├──────── Revenue by Store
      ├──────── Revenue by Product
      ├──────── Orders Timeline
      ├──────── Top Customers
      └──────── KPI Cards
```

When the user changes the date range, every component refreshes using the same filter values.

---

# API Design

Instead of one endpoint per chart, the backend may expose logical endpoints.

Examples:

```
POST /api/revenue
POST /api/orders
POST /api/customers
POST /api/kpis
POST /api/table
```

Each endpoint accepts the same filter object.

Example:

```json
{
    "from":"...",
    "to":"...",
    "stores":[...],
    "products":[...],
    "regions":[...]
}
```

---

# Performance Considerations

For dashboards querying large ClickHouse datasets:

* Use parameterized queries.
* Aggregate data in ClickHouse rather than in the browser.
* Return only the required columns.
* Apply filters in SQL rather than filtering JSON in the artifact.
* Paginate large tables.
* Cache repeated queries where appropriate.
* Limit maximum date ranges if necessary.

---

# Security

The artifact should never contain:

* ClickHouse username
* ClickHouse password
* Database host
* SQL queries with embedded credentials

These remain exclusively on the backend.

The backend should:

* Read credentials from `.env`.
* Validate all incoming filter values.
* Use parameterized SQL.
* Return only the data required by the dashboard.

---

# Recommended Technology Stack

## Frontend (LibreChat Artifact)

* HTML
* CSS
* JavaScript
* Chart.js or Apache ECharts
* Fetch API

## Backend

* Node.js
* Express
* `@clickhouse/client`
* dotenv

## Database

* ClickHouse

---

# Design Principles

1. Keep the artifact presentation-focused.
2. Keep database logic on the backend.
3. Maintain a single shared dashboard filter state.
4. Refresh visualizations by re-querying the backend rather than filtering previously fetched data.
5. Use parameterized SQL for every query.
6. Never expose database credentials or SQL execution to the browser.
