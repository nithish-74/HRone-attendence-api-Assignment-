# Employee Attendance & Analytics API

FastAPI and MongoDB backend service for managing employee punch-in/out, manual regularization audit logs, and aggregation-driven analytics pipelines.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (tested on MongoDB 7.0 / 8.0)

## Quickstart

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables** (Optional):
   ```bash
   # Default fallback is mongodb://localhost:27017 and attendance_db
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```
   *Note: Real environment variables take precedence over any `.env` file.*

3. **Seed Sample Data** (Optional for testing):
   ```bash
   python sample_seed.py
   ```

4. **Run the Application**:
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive API documentation will be accessible at: `http://localhost:8000/docs`

## Implemented Endpoints & Features

- **System**:
  - `GET /health`: Readiness probe that verifies MongoDB connectivity (returns 200 or 503).
- **Employees**:
  - `POST /employees`: Create employee with unique index enforcement and race-safety.
  - `GET /employees`: Paginated employee listing sorted by `emp_code` ascending with exact department filtering.
- **Attendance**:
  - `POST /attendance/punch-in`: Race-safe punch-in with R1 overnight shift logic and R2 late calculation.
  - `POST /attendance/punch-out`: Atomic punch-out calculation with R3 overtime, R4 work hours, and R5 half-day flag.
  - `GET /attendance`: High-performance indexed pagination sorted by `date` desc, `emp_code` asc.
  - `PATCH /attendance/{emp_code}/{date}`: Manual regularization with complete audit history and optimistic concurrency control.
- **Analytics**:
  - `GET /analytics/employees/{emp_code}/monthly`: Monthly summary backed by MongoDB aggregation (Mon-Fri working days, R5-R7 presence).
  - `GET /analytics/departments/summary`: Cross-collection department aggregation accurately accounting for employees with zero logs.
  - `GET /analytics/leaderboard/late`: Competition ranking (1, 2, 2, 4) using MongoDB `$setWindowFields` and `$rank`.
  - `GET /analytics/departments/{department}/trend`: Complete calendar gap-filling using `$densify` and 7-day moving averages in MongoDB.
- **Admin**:
  - `GET /admin/explain/{endpoint}`: Raw `executionStats` MongoDB explain plan verifying `IXSCAN` utilization across ~100k datasets.

## Key Design & Architecture Notes
- All indexes are created idempotently on server startup in `lifespan`.
- Concurrency guarantees are backed by database constraints and atomic operations (`find_one_and_update`).
- See [`REVIEW.md`](REVIEW.md) for detailed analysis of starter defects.
- See [`DECISIONS.md`](DECISIONS.md) for architectural justifications and index design rationale.
