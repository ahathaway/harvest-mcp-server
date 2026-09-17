import os
import json
import httpx
from datetime import datetime
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("harvest-api")

# Get environment variables for Harvest API
HARVEST_ACCOUNT_ID = os.environ.get("HARVEST_ACCOUNT_ID")
HARVEST_API_KEY = os.environ.get("HARVEST_API_KEY")

if not HARVEST_ACCOUNT_ID or not HARVEST_API_KEY:
    raise ValueError(
        "Missing Harvest API credentials. Set HARVEST_ACCOUNT_ID and HARVEST_API_KEY environment variables."
    )

# Read-only mode: when enabled, write operations return an error message
# instead of modifying Harvest data.
HARVEST_READ_ONLY = os.environ.get("HARVEST_READ_ONLY", "").lower() in ("true", "1", "yes")

# Sending an invoice emails a real client. It is gated separately from the general
# write flag: the operator must opt in with HARVEST_ALLOW_INVOICE_SEND, and the caller
# must additionally echo back the invoice number it intends to send.
HARVEST_ALLOW_INVOICE_SEND = os.environ.get(
    "HARVEST_ALLOW_INVOICE_SEND", ""
).lower() in ("true", "1", "yes")

SEND_DISABLED_MESSAGE = json.dumps(
    {
        "error": "invoice_send_disabled",
        "message": (
            "Sending invoices is disabled. This emails a real client, so it is gated "
            "separately from other writes. To enable, set HARVEST_ALLOW_INVOICE_SEND=true "
            "in the server environment and restart. Drafting, updating and state changes "
            "that do not email anyone are unaffected."
        ),
    },
    indent=2,
)

READ_ONLY_MESSAGE = json.dumps(
    {
        "error": "read_only_mode",
        "message": (
            "This Harvest MCP server is running in read-only mode. "
            "To enable write operations, remove the HARVEST_READ_ONLY environment variable "
            "or set it to 'false' in your MCP server configuration."
        ),
    },
    indent=2,
)


# Helper function to make Harvest API requests
async def harvest_request(path, params=None, method="GET"):
    headers = {
        "Harvest-Account-Id": HARVEST_ACCOUNT_ID,
        "Authorization": f"Bearer {HARVEST_API_KEY}",
        "User-Agent": "Harvest MCP Server",
        "Content-Type": "application/json",
    }

    url = f"https://api.harvestapp.com/v2/{path}"

    async with httpx.AsyncClient() as client:
        if method == "GET":
            response = await client.get(url, headers=headers, params=params)
        else:
            response = await client.request(method, url, headers=headers, json=params)

        if response.status_code not in (200, 201):
            raise Exception(
                f"Harvest API Error: {response.status_code} {response.text}"
            )

        # Harvest's DELETE endpoints may return 200 OK with no body. Scope
        # this fallback to DELETE only — an empty body on GET/POST/PATCH is
        # more likely a real bug that should surface as JSONDecodeError.
        if method == "DELETE" and not response.content:
            return {"status": "ok"}

        return response.json()


@mcp.tool()
async def list_users(is_active: bool = None, page: int = None, per_page: int = None):
    """List all users in your Harvest account.

    Args:
        is_active: Pass true to only return active users and false to return inactive users
        page: The page number for pagination
        per_page: The number of records to return per page (1-2000)
    """
    params = {}
    if is_active is not None:
        params["is_active"] = "true" if is_active else "false"
    else:
        params["is_active"] = "true"
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)
    else:
        params["per_page"] = 200

    response = await harvest_request("users", params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_user_details(user_id: int):
    """Retrieve details for a specific user.

    Args:
        user_id: The ID of the user to retrieve
    """
    response = await harvest_request(f"users/{user_id}")
    return json.dumps(response, indent=2)


@mcp.tool()
async def list_my_project_assignments(
    user_id: int = None, compact: bool = True, page: int = None, per_page: int = None
):
    """List the projects and tasks the authenticated user is assigned to.

    Use this to discover valid project_id / task_id pairs for create_time_entry
    and start_timer. Unlike list_projects and list_tasks, this endpoint works for
    non-admin members, so it is the reliable way to enumerate what you can log
    time against. This is the same data the Harvest web timer's project picker shows.

    Args:
        user_id: Look up another user's assignments (requires admin). Defaults to the authenticated user.
        compact: Return only project/client/task ids and names. Set false for full records with rates and budgets.
        page: The page number for pagination
        per_page: The number of records to return per page (1-2000). Defaults to 100.
    """
    params = {}
    if page is not None:
        params["page"] = str(page)
    params["per_page"] = str(per_page) if per_page is not None else "100"

    path = (
        f"users/{user_id}/project_assignments"
        if user_id is not None
        else "users/me/project_assignments"
    )
    response = await harvest_request(path, params)

    if not compact:
        return json.dumps(response, indent=2)

    # Full records run ~14 task assignments each with rates and timestamps, which
    # buries the ids the caller actually needs. Keep the picker-shaped fields only.
    assignments = [
        {
            "project_id": (a.get("project") or {}).get("id"),
            "project_name": (a.get("project") or {}).get("name"),
            "is_billable": (a.get("project") or {}).get("is_billable"),
            "client_name": (a.get("client") or {}).get("name"),
            "is_project_manager": a.get("is_project_manager"),
            "tasks": [
                {
                    "task_id": (t.get("task") or {}).get("id"),
                    "task_name": (t.get("task") or {}).get("name"),
                    "billable": t.get("billable"),
                }
                for t in (a.get("task_assignments") or [])
                if t.get("is_active")
            ],
        }
        for a in response.get("project_assignments", [])
        if a.get("is_active")
    ]

    return json.dumps(
        {
            "project_assignments": assignments,
            "page": response.get("page"),
            "total_pages": response.get("total_pages"),
            "total_entries": response.get("total_entries"),
        },
        indent=2,
    )


@mcp.tool()
async def list_time_entries(
    user_id: int = None,
    from_date: str = None,
    to_date: str = None,
    is_running: bool = None,
    is_billable: bool = None,
):
    """List time entries with optional filtering.

    Args:
        user_id: Filter by user ID
        from_date: Only return time entries with a spent_date on or after the given date (YYYY-MM-DD)
        to_date: Only return time entries with a spent_date on or before the given date (YYYY-MM-DD)
        is_running: Pass true to only return running time entries and false to return non-running time entries
        is_billable: Pass true to only return billable time entries and false to return non-billable time entries
    """
    params = {}
    if user_id is not None:
        params["user_id"] = str(user_id)
    if from_date is not None:
        params["from"] = from_date
    if to_date is not None:
        params["to"] = to_date
    if is_running is not None:
        params["is_running"] = "true" if is_running else "false"
    if is_billable is not None:
        params["is_billable"] = "true" if is_billable else "false"

    response = await harvest_request("time_entries", params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_company():
    """Retrieve company settings for the authenticated Harvest account.

    Check `wants_timestamp_timers` before creating or updating time entries:
    when true, Harvest derives duration from started_time/ended_time and ignores
    the `hours` field entirely.
    """
    response = await harvest_request("company")
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_time_entry(time_entry_id: int):
    """Retrieve one time entry by id.

    Useful after a create or update to confirm what Harvest actually stored —
    accounts configured for timestamp timers can store a different shape than
    the one requested.

    Args:
        time_entry_id: The ID of the time entry to retrieve
    """
    response = await harvest_request(f"time_entries/{time_entry_id}")
    return json.dumps(response, indent=2)


@mcp.tool()
async def update_time_entry(
    time_entry_id: int,
    project_id: int = None,
    task_id: int = None,
    spent_date: str = None,
    hours: float = None,
    started_time: str = None,
    ended_time: str = None,
    notes: str | int | None = None,
):
    """Update an existing time entry. Only the fields you pass are changed.

    Use this to correct hours, notes, or to move an entry to a different project
    or task.

    IMPORTANT: on accounts with timestamp timers enabled (company.wants_timestamp_timers
    is true), Harvest IGNORES `hours` on both create and update — duration is derived
    from the clock. On those accounts pass started_time and ended_time instead.
    Check the company setting first if an hours update appears to do nothing.

    Args:
        time_entry_id: The ID of the time entry to update
        project_id: Move the entry to this project
        task_id: Move the entry to this task
        spent_date: The date the time was spent (YYYY-MM-DD)
        hours: The number of hours spent. Ignored on timestamp-timer accounts.
        started_time: Start of the entry, e.g. "8:00am". Timestamp-timer accounts only.
        ended_time: End of the entry, e.g. "3:30pm". Timestamp-timer accounts only.
        notes: Notes about the time entry
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {}
    if project_id is not None:
        params["project_id"] = project_id
    if task_id is not None:
        params["task_id"] = task_id
    if spent_date is not None:
        params["spent_date"] = spent_date
    if hours is not None:
        params["hours"] = hours
    if started_time is not None:
        params["started_time"] = started_time
    if ended_time is not None:
        params["ended_time"] = ended_time
    if notes is not None:
        params["notes"] = str(notes)

    if not params:
        return json.dumps(
            {"error": "no_fields", "message": "Pass at least one field to update."}, indent=2
        )

    response = await harvest_request(
        f"time_entries/{time_entry_id}", params, method="PATCH"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def delete_time_entry(time_entry_id: int):
    """Delete a time entry permanently.

    Args:
        time_entry_id: The ID of the time entry to delete
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(f"time_entries/{time_entry_id}", method="DELETE")
    return json.dumps(response, indent=2)


@mcp.tool()
async def restart_time_entry(time_entry_id: int):
    """Restart a stopped time entry, making it the running timer again.

    Args:
        time_entry_id: The ID of the stopped time entry to restart
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"time_entries/{time_entry_id}/restart", method="PATCH"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def create_time_entry(
    project_id: int,
    task_id: int,
    spent_date: str,
    hours: float = None,
    started_time: str = None,
    ended_time: str = None,
    notes: str | int | None = None,
):
    """Create a new time entry.

    IMPORTANT: on accounts with timestamp timers enabled (company.wants_timestamp_timers
    is true), Harvest IGNORES `hours` and instead starts a RUNNING TIMER at 0 hours.
    On those accounts pass started_time and ended_time to log a completed entry.
    Check the company setting before creating entries in bulk, or you will leave a
    trail of running timers.

    Args:
        project_id: The ID of the project to associate with the time entry
        task_id: The ID of the task to associate with the time entry
        spent_date: The date when the time was spent (YYYY-MM-DD)
        hours: The number of hours spent. Ignored on timestamp-timer accounts.
        started_time: Start of the entry, e.g. "8:00am". Timestamp-timer accounts only.
        ended_time: End of the entry, e.g. "3:30pm". Timestamp-timer accounts only.
        notes: Optional notes about the time entry
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {
        "project_id": project_id,
        "task_id": task_id,
        "spent_date": spent_date,
    }

    if hours is not None:
        params["hours"] = hours
    if started_time is not None:
        params["started_time"] = started_time
    if ended_time is not None:
        params["ended_time"] = ended_time
    if notes is not None:
        params["notes"] = str(notes)

    response = await harvest_request("time_entries", params, method="POST")
    return json.dumps(response, indent=2)


@mcp.tool()
async def stop_timer(time_entry_id: int):
    """Stop a running timer.

    Args:
        time_entry_id: The ID of the running time entry to stop
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"time_entries/{time_entry_id}/stop", method="PATCH"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def start_timer(
    project_id: int,
    task_id: int,
    notes: str | int | None = None,
):
    """Start a new timer.

    Args:
        project_id: The ID of the project to associate with the time entry
        task_id: The ID of the task to associate with the time entry
        notes: Optional notes about the time entry
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {
        "project_id": project_id,
        "task_id": task_id,
        "spent_date": datetime.now().strftime("%Y-%m-%d"),
    }

    if notes is not None:
        params["notes"] = str(notes)

    response = await harvest_request("time_entries", params, method="POST")
    return json.dumps(response, indent=2)


@mcp.tool()
async def list_projects(
    client_id: int = None,
    is_active: bool = None,
    updated_since: str = None,
    page: int = None,
    per_page: int = None,
):
    """List projects with optional filtering.

    Args:
        client_id: Only return projects belonging to the client with the given ID
        is_active: Pass true to only return active projects and false to return inactive projects
        updated_since: Only return projects updated since the given datetime (e.g. 2021-04-09T12:48:29Z)
        page: The page number to use in pagination (default: 1). Deprecated by Harvest
            in favor of cursor-based pagination via the response's links.next URL.
        per_page: The number of records to return per page (1-2000, default: 2000)
    """
    params = {}
    if client_id is not None:
        params["client_id"] = str(client_id)
    if is_active is not None:
        params["is_active"] = "true" if is_active else "false"
    if updated_since is not None:
        params["updated_since"] = updated_since
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)

    response = await harvest_request("projects", params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_project_details(project_id: int):
    """Get detailed information about a specific project.

    Args:
        project_id: The ID of the project to retrieve
    """
    response = await harvest_request(f"projects/{project_id}")
    return json.dumps(response, indent=2)


@mcp.tool()
async def create_project(
    client_id: int,
    name: str,
    is_billable: bool,
    bill_by: str,
    budget_by: str,
    code: str = None,
    is_active: bool = None,
    is_fixed_fee: bool = None,
    hourly_rate: float = None,
    budget_is_monthly: bool = None,
    budget: float = None,
    cost_budget: float = None,
    cost_budget_include_expenses: bool = None,
    notify_when_over_budget: bool = None,
    over_budget_notification_percentage: float = None,
    show_budget_to_all: bool = None,
    fee: float = None,
    notes: str = None,
    starts_on: str = None,
    ends_on: str = None,
):
    """Create a new project.

    Empirical findings (not in Harvest's docs):

    1. budget_by acceptance is unpredictable from the API surface alone.
       Only "none" is unconditionally accepted. Every other value
       ("project", "project_cost", "task", "task_fees", "person") has
       been observed to silently coerce to "none" (HTTP 200, no error)
       in at least one tested project/account state. The behavior
       depends on the account's plan tier, the authenticated user's
       role, the project's existing assignments, and (per finding 2
       below) whether you pair the value with its companion budget /
       cost_budget field in the same request. If you depend on a
       specific budget_by, re-read after the call and verify it persisted.

    2. budget-cluster coupling. The fields budget, cost_budget,
       cost_budget_include_expenses, and budget_is_monthly are silently
       zeroed/nulled if the project's resulting budget_by is "none". To
       activate any of them, send budget_by="project" (for budget hours)
       or "project_cost" (for cost_budget money) in the SAME request —
       sending budget_by alone, without the cluster field, frequently
       results in coerce-to-none.

    Args:
        client_id: The ID of the client to associate this project with (required)
        name: The name of the project (required)
        is_billable: Whether the project is billable or not (required)
        bill_by: The method by which the project is invoiced (required). One of:
            "Project", "Tasks", "People", "none". Note the mixed casing — Harvest's
            API expects exactly these values. (Distinct from budget_by, which is
            all-lowercase.)
        budget_by: The method by which the project is budgeted (required). One of:
            "project", "project_cost", "task", "task_fees", "person", "none".
            Note this is all-lowercase, unlike bill_by. See the function-level
            note above about which values round-trip reliably.
        code: The code associated with the project. Note: passing an empty
            string ("") clears the field to null rather than storing ""
        is_active: Whether the project is active or archived. Defaults to true
        is_fixed_fee: Whether the project is a fixed-fee project or not
        hourly_rate: Rate for projects billed by Project Hourly Rate
        budget_is_monthly: Option to have the budget reset every month. Defaults
            to false. Silently ignored if budget_by="none" — see note above
        budget: The budget in HOURS for the project when budgeting by time
            (i.e. budget_by="project", "task", or "person"). Use cost_budget for
            money-based budgets. Silently nulled if budget_by="none"
        cost_budget: The MONETARY budget for the project when budgeting by money
            (i.e. budget_by="project_cost" or "task_fees"). Use budget for
            hours-based budgets. Silently nulled if budget_by="none"
        cost_budget_include_expenses: Option for budget of Total Project Fees
            projects to include tracked expenses. Defaults to false. Silently
            ignored if budget_by="none"
        notify_when_over_budget: Whether Project Managers should be notified when
            the project goes over budget. Defaults to false
        over_budget_notification_percentage: Percentage value used to trigger
            over-budget email alerts (e.g. 10.0 for 10.0%)
        show_budget_to_all: Option to show project budget to all employees.
            Defaults to false. Does not apply to Total Project Fee projects
        fee: The amount you plan to invoice for the project. Only used by
            fixed-fee projects
        notes: Project notes
        starts_on: Date the project was started (YYYY-MM-DD)
        ends_on: Date the project will end (YYYY-MM-DD)
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {
        "client_id": client_id,
        "name": name,
        "is_billable": is_billable,
        "bill_by": bill_by,
        "budget_by": budget_by,
    }
    if code is not None:
        params["code"] = code
    if is_active is not None:
        params["is_active"] = is_active
    if is_fixed_fee is not None:
        params["is_fixed_fee"] = is_fixed_fee
    if hourly_rate is not None:
        params["hourly_rate"] = hourly_rate
    if budget_is_monthly is not None:
        params["budget_is_monthly"] = budget_is_monthly
    if budget is not None:
        params["budget"] = budget
    if cost_budget is not None:
        params["cost_budget"] = cost_budget
    if cost_budget_include_expenses is not None:
        params["cost_budget_include_expenses"] = cost_budget_include_expenses
    if notify_when_over_budget is not None:
        params["notify_when_over_budget"] = notify_when_over_budget
    if over_budget_notification_percentage is not None:
        params["over_budget_notification_percentage"] = over_budget_notification_percentage
    if show_budget_to_all is not None:
        params["show_budget_to_all"] = show_budget_to_all
    if fee is not None:
        params["fee"] = fee
    if notes is not None:
        params["notes"] = notes
    if starts_on is not None:
        params["starts_on"] = starts_on
    if ends_on is not None:
        params["ends_on"] = ends_on

    response = await harvest_request("projects", params, method="POST")
    return json.dumps(response, indent=2)


@mcp.tool()
async def update_project(
    project_id: int,
    client_id: int = None,
    name: str = None,
    code: str = None,
    is_active: bool = None,
    is_billable: bool = None,
    is_fixed_fee: bool = None,
    bill_by: str = None,
    hourly_rate: float = None,
    budget_by: str = None,
    budget_is_monthly: bool = None,
    budget: float = None,
    cost_budget: float = None,
    cost_budget_include_expenses: bool = None,
    notify_when_over_budget: bool = None,
    over_budget_notification_percentage: float = None,
    show_budget_to_all: bool = None,
    fee: float = None,
    notes: str = None,
    starts_on: str = None,
    ends_on: str = None,
):
    """Update an existing project.

    Only the parameters you provide are changed; omitted parameters
    remain untouched.

    To archive a project (instead of deleting it and losing all its
    time entries and expenses), pass is_active=False.

    Empirical findings (not in Harvest's docs):

    1. budget_by acceptance is unpredictable from the API surface alone.
       Only "none" is unconditionally accepted. Every other value
       ("project", "project_cost", "task", "task_fees", "person") has
       been observed to silently coerce to "none" (HTTP 200, no error)
       in at least one tested project/account state. The behavior
       depends on the account's plan tier, the authenticated user's
       role, the project's existing assignments, and (per finding 2
       below) whether you pair the value with its companion budget /
       cost_budget field in the same request. Re-read after the call
       and verify it persisted.

    2. budget-cluster coupling. The fields budget, cost_budget,
       cost_budget_include_expenses, and budget_is_monthly are silently
       zeroed/nulled when the project's resulting budget_by is "none". To
       set or change any of them, send budget_by="project" (for budget
       hours) or "project_cost" (for cost_budget money) in the SAME PATCH;
       sending the cluster field alone with stored budget_by="none" is a
       no-op even though the response is 200. Sending budget_by alone,
       without the cluster field, frequently results in coerce-to-none.

    3. Empty-string clearing differs by field. Passing code="" clears the
       value to null on the server. Passing notes="" round-trips as the
       empty string. The two text fields use different clearing semantics.

    Args:
        project_id: The ID of the project to update
        client_id: The ID of the client to associate this project with
        name: The name of the project
        code: The code associated with the project. Passing "" clears the
            field to null
        is_active: Whether the project is active or archived. Pass False
            to archive — this is the recommended alternative to delete_project
        is_billable: Whether the project is billable or not
        is_fixed_fee: Whether the project is a fixed-fee project or not
        bill_by: The method by which the project is invoiced. One of:
            "Project", "Tasks", "People", "none". Note the mixed casing —
            distinct from budget_by, which is all-lowercase
        hourly_rate: Rate for projects billed by Project Hourly Rate
        budget_by: The method by which the project is budgeted. One of:
            "project", "project_cost", "task", "task_fees", "person", "none".
            Note this is all-lowercase, unlike bill_by. See the function-level
            empirical note about which values round-trip reliably.
        budget_is_monthly: Option to have the budget reset every month.
            Silently ignored if budget_by ends up as "none"
        budget: The budget in HOURS for the project when budgeting by time
            (i.e. budget_by="project", "task", or "person"). Silently nulled
            if budget_by ends up as "none" — pair with budget_by in the same
            call to set this field
        cost_budget: The MONETARY budget for the project when budgeting by
            money (i.e. budget_by="project_cost" or "task_fees"). Silently
            nulled if budget_by ends up as "none" — pair with budget_by in
            the same call to set this field
        cost_budget_include_expenses: Option for budget of Total Project
            Fees projects to include tracked expenses. Silently ignored if
            budget_by ends up as "none"
        notify_when_over_budget: Whether Project Managers should be notified
            when the project goes over budget
        over_budget_notification_percentage: Percentage value used to
            trigger over-budget email alerts (e.g. 10.0 for 10.0%)
        show_budget_to_all: Option to show project budget to all employees.
            Does not apply to Total Project Fee projects
        fee: The amount you plan to invoice for the project. Only used by
            fixed-fee projects
        notes: Project notes. Passing "" stores the empty string (unlike code)
        starts_on: Date the project was started (YYYY-MM-DD)
        ends_on: Date the project will end (YYYY-MM-DD)
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {}
    if client_id is not None:
        params["client_id"] = client_id
    if name is not None:
        params["name"] = name
    if code is not None:
        params["code"] = code
    if is_active is not None:
        params["is_active"] = is_active
    if is_billable is not None:
        params["is_billable"] = is_billable
    if is_fixed_fee is not None:
        params["is_fixed_fee"] = is_fixed_fee
    if bill_by is not None:
        params["bill_by"] = bill_by
    if hourly_rate is not None:
        params["hourly_rate"] = hourly_rate
    if budget_by is not None:
        params["budget_by"] = budget_by
    if budget_is_monthly is not None:
        params["budget_is_monthly"] = budget_is_monthly
    if budget is not None:
        params["budget"] = budget
    if cost_budget is not None:
        params["cost_budget"] = cost_budget
    if cost_budget_include_expenses is not None:
        params["cost_budget_include_expenses"] = cost_budget_include_expenses
    if notify_when_over_budget is not None:
        params["notify_when_over_budget"] = notify_when_over_budget
    if over_budget_notification_percentage is not None:
        params["over_budget_notification_percentage"] = over_budget_notification_percentage
    if show_budget_to_all is not None:
        params["show_budget_to_all"] = show_budget_to_all
    if fee is not None:
        params["fee"] = fee
    if notes is not None:
        params["notes"] = notes
    if starts_on is not None:
        params["starts_on"] = starts_on
    if ends_on is not None:
        params["ends_on"] = ends_on

    response = await harvest_request(f"projects/{project_id}", params, method="PATCH")
    return json.dumps(response, indent=2)


@mcp.tool()
async def delete_project(project_id: int):
    """Delete a project.

    DESTRUCTIVE: deletes the project AND all time entries and expenses
    tracked to it. Invoices associated with the project are NOT deleted.

    If you want to retain the project's time entries and expenses,
    archive the project instead by calling update_project with
    is_active=False — this is Harvest's documented recommendation.

    Args:
        project_id: The ID of the project to delete
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(f"projects/{project_id}", method="DELETE")
    return json.dumps(response, indent=2)


@mcp.tool()
async def list_task_assignments(
    project_id: int = None,
    is_active: bool = None,
    updated_since: str = None,
    page: int = None,
    per_page: int = None,
):
    """List task assignments with optional filtering.

    A task assignment links a Harvest task to a project, with optional
    per-assignment billable flag, hourly rate, and budget.

    Without project_id, lists task assignments across the whole account
    (GET /v2/task_assignments). With project_id, lists task assignments
    for that project only (GET /v2/projects/{id}/task_assignments).

    Args:
        project_id: If provided, scope to this project's task assignments.
            If omitted, list all task assignments in the account.
        is_active: Pass true to only return active task assignments and
            false to return inactive ones
        updated_since: Only return task assignments updated since the
            given datetime (e.g. 2021-04-09T12:48:29Z)
        page: The page number to use in pagination (default: 1). Deprecated
            by Harvest in favor of cursor-based pagination via the response's
            links.next URL.
        per_page: The number of records to return per page (1-2000, default: 2000)
    """
    params = {}
    if is_active is not None:
        params["is_active"] = "true" if is_active else "false"
    if updated_since is not None:
        params["updated_since"] = updated_since
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)

    path = (
        f"projects/{project_id}/task_assignments"
        if project_id is not None
        else "task_assignments"
    )
    response = await harvest_request(path, params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_task_assignment_details(project_id: int, task_assignment_id: int):
    """Get detailed information about a specific task assignment.

    Args:
        project_id: The ID of the project the task assignment belongs to
        task_assignment_id: The ID of the task assignment to retrieve
    """
    response = await harvest_request(
        f"projects/{project_id}/task_assignments/{task_assignment_id}"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def create_task_assignment(
    project_id: int,
    task_id: int,
    is_active: bool = None,
    billable: bool = None,
    hourly_rate: float = None,
    budget: float = None,
):
    """Create a task assignment, linking a task to a project.

    Empirical findings (not in Harvest's docs):

    1. Duplicate-task UPSERT. POSTing with a task_id that is already
       assigned to this project does NOT return 422. Harvest returns 201
       with the existing task_assignment id and applies any provided
       fields the project's bill_by/budget_by accept (so e.g. is_active
       and billable always apply; hourly_rate and budget apply only when
       the parent project's bill_by/budget_by match — otherwise they
       follow the standard silent-null rules described per-field below).
       Callers expecting a conflict error will instead silently mutate
       state. Use update_task_assignment deliberately when you mean to
       change an existing one.

    2. billable default comes from the task, not the spec. Harvest's spec
       says billable defaults to false when omitted, but in practice it
       defaults to the parent task's billable_by_default. Pass billable
       explicitly if you depend on a specific value.

    Args:
        project_id: The ID of the project to assign the task to (required)
        task_id: The ID of the task to associate with the project (required)
        is_active: Whether the task assignment is active or archived.
            Defaults to true
        billable: Whether the task assignment is billable or not. See the
            empirical note above about Harvest's actual default behavior
        hourly_rate: Custom rate used when the project's bill_by is "Tasks".
            Silently nulled in the response if the project's bill_by is any
            other value — no error raised
        budget: Per-task-assignment budget. Used when the project's
            budget_by is "task" or "task_fees". Silently nulled in the
            response if budget_by is any other value — no error raised
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {"task_id": task_id}
    if is_active is not None:
        params["is_active"] = is_active
    if billable is not None:
        params["billable"] = billable
    if hourly_rate is not None:
        params["hourly_rate"] = hourly_rate
    if budget is not None:
        params["budget"] = budget

    response = await harvest_request(
        f"projects/{project_id}/task_assignments", params, method="POST"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def update_task_assignment(
    project_id: int,
    task_assignment_id: int,
    is_active: bool = None,
    billable: bool = None,
    hourly_rate: float = None,
    budget: float = None,
):
    """Update an existing task assignment.

    Only the parameters you provide are changed; omitted parameters
    remain untouched.

    Args:
        project_id: The ID of the project the task assignment belongs to
        task_assignment_id: The ID of the task assignment to update
        is_active: Whether the task assignment is active or archived
        billable: Whether the task assignment is billable or not. When
            true, time tracked against this task on this project is marked
            billable
        hourly_rate: Custom rate used when the project's bill_by is "Tasks".
            Silently nulled in the response if the project's bill_by is any
            other value — no error raised
        budget: Per-task-assignment budget. Used when the project's
            budget_by is "task" or "task_fees". Silently nulled in the
            response if budget_by is any other value — no error raised
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {}
    if is_active is not None:
        params["is_active"] = is_active
    if billable is not None:
        params["billable"] = billable
    if hourly_rate is not None:
        params["hourly_rate"] = hourly_rate
    if budget is not None:
        params["budget"] = budget

    response = await harvest_request(
        f"projects/{project_id}/task_assignments/{task_assignment_id}",
        params,
        method="PATCH",
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def delete_task_assignment(project_id: int, task_assignment_id: int):
    """Delete a task assignment.

    Per Harvest's docs, deletion is only possible if the task assignment
    has no time entries logged against it. If time entries exist, the
    API returns HTTP 422 with the message "This task assignment isn’t
    removable because there are time entries associated with it." (note
    the typographic apostrophe U+2019 in "isn’t") and nothing is
    deleted. In that case, archive the task assignment instead via
    update_task_assignment(is_active=False).

    Args:
        project_id: The ID of the project the task assignment belongs to
        task_assignment_id: The ID of the task assignment to delete
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"projects/{project_id}/task_assignments/{task_assignment_id}",
        method="DELETE",
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def list_user_assignments(
    project_id: int = None,
    user_id: int = None,
    is_active: bool = None,
    updated_since: str = None,
    page: int = None,
    per_page: int = None,
):
    """List user assignments with optional filtering.

    A user assignment links a Harvest user to a project, with optional
    project-manager flag, billable rates (custom or default), and budget.

    Without project_id, lists user assignments across the whole account
    (GET /v2/user_assignments). With project_id, lists user assignments
    for that project only (GET /v2/projects/{id}/user_assignments).

    Args:
        project_id: If provided, scope to this project's user assignments.
            If omitted, list all user assignments in the account.
        user_id: Only return user assignments belonging to the user with
            the given ID. Available on the account-wide endpoint and the
            project-scoped endpoint
        is_active: Pass true to only return active user assignments and
            false to return inactive ones
        updated_since: Only return user assignments updated since the
            given datetime (e.g. 2021-04-09T12:48:29Z)
        page: The page number to use in pagination (default: 1). Deprecated
            by Harvest in favor of cursor-based pagination via the response's
            links.next URL.
        per_page: The number of records to return per page (1-2000, default: 2000)
    """
    params = {}
    if user_id is not None:
        params["user_id"] = str(user_id)
    if is_active is not None:
        params["is_active"] = "true" if is_active else "false"
    if updated_since is not None:
        params["updated_since"] = updated_since
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)

    path = (
        f"projects/{project_id}/user_assignments"
        if project_id is not None
        else "user_assignments"
    )
    response = await harvest_request(path, params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_user_assignment_details(project_id: int, user_assignment_id: int):
    """Get detailed information about a specific user assignment.

    Args:
        project_id: The ID of the project the user assignment belongs to
        user_assignment_id: The ID of the user assignment to retrieve
    """
    response = await harvest_request(
        f"projects/{project_id}/user_assignments/{user_assignment_id}"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def create_user_assignment(
    project_id: int,
    user_id: int,
    is_active: bool = None,
    is_project_manager: bool = None,
    use_default_rates: bool = None,
    hourly_rate: float = None,
    budget: float = None,
):
    """Create a user assignment, linking a user to a project.

    Empirical finding (not in Harvest's docs): POSTing with a user_id
    that is already assigned to this project does NOT return 422.
    Harvest returns 201 with the existing user_assignment id, and any
    fields you provide on the request are silently dropped — the
    response and a subsequent GET both show the assignment unchanged.
    To change an existing user assignment, use update_user_assignment.

    Args:
        project_id: The ID of the project to assign the user to (required)
        user_id: The ID of the user to associate with the project (required)
        is_active: Whether the user assignment is active or archived.
            Defaults to true
        is_project_manager: Whether the user has Project Manager
            permissions for this project. Per Harvest's docs, defaults to
            false for regular users and true for users who are already
            account-wide Project Managers or Administrators
        use_default_rates: Whether to use the user's account-default
            billable rates for this project (true) or a custom rate
            defined on this assignment (false). Defaults to true. Only
            relevant when the project's bill_by is "People" — silently
            ignored / coerced back to true when bill_by is any other value
        hourly_rate: Custom rate used when the project's bill_by is
            "People" AND use_default_rates is false. Silently nulled in
            the response if the project's bill_by is any other value —
            no error raised. Note: when bill_by is "People" and
            use_default_rates is true, the response shows the user's
            account-default billable rate, not 0
        budget: Per-user-assignment budget. Used when the project's
            budget_by is "person". Silently nulled in the response if
            budget_by is any other value — no error raised
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {"user_id": user_id}
    if is_active is not None:
        params["is_active"] = is_active
    if is_project_manager is not None:
        params["is_project_manager"] = is_project_manager
    if use_default_rates is not None:
        params["use_default_rates"] = use_default_rates
    if hourly_rate is not None:
        params["hourly_rate"] = hourly_rate
    if budget is not None:
        params["budget"] = budget

    response = await harvest_request(
        f"projects/{project_id}/user_assignments", params, method="POST"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def update_user_assignment(
    project_id: int,
    user_assignment_id: int,
    is_active: bool = None,
    is_project_manager: bool = None,
    use_default_rates: bool = None,
    hourly_rate: float = None,
    budget: float = None,
):
    """Update an existing user assignment.

    Only the parameters you provide are changed; omitted parameters
    remain untouched.

    Args:
        project_id: The ID of the project the user assignment belongs to
        user_assignment_id: The ID of the user assignment to update
        is_active: Whether the user assignment is active or archived
        is_project_manager: Whether the user has Project Manager
            permissions for this project
        use_default_rates: Whether to use the user's account-default
            billable rates (true) or a custom rate defined on this
            assignment (false). Only relevant when the project's bill_by
            is "People" — silently ignored / coerced back to true when
            bill_by is any other value
        hourly_rate: Custom rate used when the project's bill_by is
            "People" AND use_default_rates is false. Silently nulled in
            the response if the project's bill_by is any other value —
            no error raised
        budget: Per-user-assignment budget. Used when the project's
            budget_by is "person". Silently nulled in the response if
            budget_by is any other value — no error raised
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {}
    if is_active is not None:
        params["is_active"] = is_active
    if is_project_manager is not None:
        params["is_project_manager"] = is_project_manager
    if use_default_rates is not None:
        params["use_default_rates"] = use_default_rates
    if hourly_rate is not None:
        params["hourly_rate"] = hourly_rate
    if budget is not None:
        params["budget"] = budget

    response = await harvest_request(
        f"projects/{project_id}/user_assignments/{user_assignment_id}",
        params,
        method="PATCH",
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def delete_user_assignment(project_id: int, user_assignment_id: int):
    """Delete a user assignment.

    Per Harvest's docs, deletion is only possible if the user assignment
    has no time entries OR expenses logged against it. If either exists,
    the API returns HTTP 422 with the message "This task assignment
    isn’t removable because it has tracked time or expenses." (note two
    Harvest-side quirks: the message uses a typographic apostrophe
    U+2019, and it says "task assignment" even though the endpoint is
    user_assignments). Nothing is deleted in that case. To retain
    history, archive the user assignment instead via
    update_user_assignment(is_active=False).

    Args:
        project_id: The ID of the project the user assignment belongs to
        user_assignment_id: The ID of the user assignment to delete
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"projects/{project_id}/user_assignments/{user_assignment_id}",
        method="DELETE",
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def list_clients(is_active: bool = None):
    """List clients with optional filtering.

    Args:
        is_active: Pass true to only return active clients and false to return inactive clients
    """
    params = {}
    if is_active is not None:
        params["is_active"] = "true" if is_active else "false"

    response = await harvest_request("clients", params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_client_details(client_id: int):
    """Get detailed information about a specific client.

    Args:
        client_id: The ID of the client to retrieve
    """
    response = await harvest_request(f"clients/{client_id}")
    return json.dumps(response, indent=2)


@mcp.tool()
async def list_tasks(is_active: bool = None):
    """List all tasks with optional filtering.

    Args:
        is_active: Pass true to only return active tasks and false to return inactive tasks
    """
    params = {}
    if is_active is not None:
        params["is_active"] = "true" if is_active else "false"

    response = await harvest_request("tasks", params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_unsubmitted_timesheets(
    user_id: int = None,
    from_date: str = None,
    to_date: str = None,
    page: int = None,
    per_page: int = None,
):
    """Get unsubmitted timesheets (time entries that haven't been submitted for approval).

    This function queries for time entries that are not yet closed/submitted, which typically
    means they are still editable and haven't been submitted for approval or invoicing.

    Args:
        user_id: Filter by specific user ID (optional)
        from_date: Only return time entries with a spent_date on or after the given date (YYYY-MM-DD)
        to_date: Only return time entries with a spent_date on or before the given date (YYYY-MM-DD)
        page: The page number for pagination
        per_page: The number of records to return per page (1-2000)
    """
    params = {}
    if user_id is not None:
        params["user_id"] = str(user_id)
    if from_date is not None:
        params["from"] = from_date
    if to_date is not None:
        params["to"] = to_date
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)
    else:
        params["per_page"] = "200"

    # Get all time entries first
    response = await harvest_request("time_entries", params)

    # Filter for unsubmitted entries (those that are not closed)
    unsubmitted_entries = []
    if "time_entries" in response:
        for entry in response["time_entries"]:
            # Time entries that are not closed are considered unsubmitted
            if not entry.get("is_closed", False):
                unsubmitted_entries.append(entry)

    # Create a response structure similar to the original API response
    filtered_response = {
        "time_entries": unsubmitted_entries,
        "per_page": response.get("per_page", len(unsubmitted_entries)),
        "total_pages": 1,  # Simplified since we're filtering client-side
        "total_entries": len(unsubmitted_entries),
        "next_page": None,
        "previous_page": None,
        "page": response.get("page", 1),
        "links": response.get("links", {}),
    }

    return json.dumps(filtered_response, indent=2)


# --- Invoices ------------------------------------------------------------
#
# Harvest has no dedicated line-item endpoints: items are created, changed and
# removed by PATCHing the invoice with a `line_items` array. Deletion is a member
# carrying `_destroy: true`. The helpers below wrap that so callers never have to
# hand-build the array.
#
# Invoice endpoints require administrator or manager permissions. On an account
# where the user is a regular member every call here returns "Not authorized!".


def _line_items_payload(line_items):
    """Pass a list of line-item dicts through unchanged, rejecting a bad shape early."""
    if not isinstance(line_items, list) or not all(
        isinstance(li, dict) for li in line_items
    ):
        raise ValueError("line_items must be a list of objects")
    return line_items


@mcp.tool()
async def list_invoices(
    client_id: int = None,
    project_id: int = None,
    state: str = None,
    from_date: str = None,
    to_date: str = None,
    updated_since: str = None,
    page: int = None,
    per_page: int = None,
):
    """List invoices, newest first.

    Args:
        client_id: Only invoices belonging to this client
        project_id: Only invoices belonging to this project
        state: Filter by state: draft, open, paid, or closed
        from_date: Only invoices with an issue date on or after this date (YYYY-MM-DD)
        to_date: Only invoices with an issue date on or before this date (YYYY-MM-DD)
        updated_since: Only invoices updated since this UTC timestamp
        page: The page number for pagination
        per_page: The number of records to return per page (1-2000)
    """
    params = {}
    if client_id is not None:
        params["client_id"] = str(client_id)
    if project_id is not None:
        params["project_id"] = str(project_id)
    if state is not None:
        params["state"] = state
    if from_date is not None:
        params["from"] = from_date
    if to_date is not None:
        params["to"] = to_date
    if updated_since is not None:
        params["updated_since"] = updated_since
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)

    response = await harvest_request("invoices", params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_invoice(invoice_id: int):
    """Retrieve one invoice, including its line items and totals.

    Args:
        invoice_id: The ID of the invoice to retrieve
    """
    response = await harvest_request(f"invoices/{invoice_id}")
    return json.dumps(response, indent=2)


@mcp.tool()
async def list_invoice_messages(
    invoice_id: int,
    updated_since: str = None,
    page: int = None,
    per_page: int = None,
):
    """List the messages sent for an invoice, including state-change events.

    Args:
        invoice_id: The ID of the invoice
        updated_since: Only messages updated since this UTC timestamp
        page: The page number for pagination
        per_page: The number of records to return per page (1-2000)
    """
    params = {}
    if updated_since is not None:
        params["updated_since"] = updated_since
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)

    response = await harvest_request(f"invoices/{invoice_id}/messages", params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def list_invoice_item_categories(
    updated_since: str = None, page: int = None, per_page: int = None
):
    """List invoice item categories, which classify line items.

    Args:
        updated_since: Only categories updated since this UTC timestamp
        page: The page number for pagination
        per_page: The number of records to return per page (1-2000)
    """
    params = {}
    if updated_since is not None:
        params["updated_since"] = updated_since
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)

    response = await harvest_request("invoice_item_categories", params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_invoice_item_category(invoice_item_category_id: int):
    """Retrieve one invoice item category.

    Args:
        invoice_item_category_id: The ID of the category to retrieve
    """
    response = await harvest_request(
        f"invoice_item_categories/{invoice_item_category_id}"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def create_invoice(
    client_id: int,
    line_items: list = None,
    subject: str = None,
    notes: str = None,
    number: str = None,
    purchase_order: str = None,
    currency: str = None,
    issue_date: str = None,
    due_date: str = None,
    payment_term: str = None,
    tax: float = None,
    tax2: float = None,
    discount: float = None,
    estimate_id: int = None,
    retainer_id: int = None,
):
    """Create a free-form invoice. It is created as a DRAFT and is not sent to anyone.

    Args:
        client_id: The ID of the client this invoice belongs to (required)
        line_items: List of line-item objects, each with kind, description, unit_price,
            quantity, and optionally taxed / taxed2 / project_id.
            Example: [{"kind": "Service", "description": "Consulting",
            "unit_price": 150, "quantity": 8, "taxed": false}]
        subject: The invoice subject
        notes: Notes shown on the invoice
        number: Invoice number. Harvest assigns the next one when omitted.
        purchase_order: The client's purchase order number
        currency: ISO currency code, e.g. USD. Defaults to the client's currency.
        issue_date: Date the invoice is issued (YYYY-MM-DD). Defaults to today.
        due_date: Date payment is due (YYYY-MM-DD)
        payment_term: One of upon receipt, net 15, net 30, net 45, net 60, or custom
        tax: First tax rate as a percentage
        tax2: Second tax rate as a percentage
        discount: Discount as a percentage
        estimate_id: Associate the invoice with this estimate
        retainer_id: Associate the invoice with this retainer
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {"client_id": client_id}
    if line_items is not None:
        try:
            params["line_items"] = _line_items_payload(line_items)
        except ValueError as exc:
            return json.dumps({"error": "invalid_line_items", "message": str(exc)}, indent=2)
    for key, value in (
        ("subject", subject),
        ("notes", notes),
        ("number", number),
        ("purchase_order", purchase_order),
        ("currency", currency),
        ("issue_date", issue_date),
        ("due_date", due_date),
        ("payment_term", payment_term),
        ("tax", tax),
        ("tax2", tax2),
        ("discount", discount),
        ("estimate_id", estimate_id),
        ("retainer_id", retainer_id),
    ):
        if value is not None:
            params[key] = value

    response = await harvest_request("invoices", params, method="POST")
    return json.dumps(response, indent=2)


@mcp.tool()
async def update_invoice(
    invoice_id: int,
    client_id: int = None,
    subject: str = None,
    notes: str = None,
    number: str = None,
    purchase_order: str = None,
    currency: str = None,
    issue_date: str = None,
    due_date: str = None,
    payment_term: str = None,
    tax: float = None,
    tax2: float = None,
    discount: float = None,
    estimate_id: int = None,
    retainer_id: int = None,
):
    """Update an invoice's own fields. Only the fields you pass are changed.

    Line items are not touched here — use add_invoice_line_items,
    update_invoice_line_item, or delete_invoice_line_item.

    Args:
        invoice_id: The ID of the invoice to update
        client_id: Move the invoice to this client
        subject: The invoice subject
        notes: Notes shown on the invoice
        number: Invoice number
        purchase_order: The client's purchase order number
        currency: ISO currency code, e.g. USD
        issue_date: Date the invoice is issued (YYYY-MM-DD)
        due_date: Date payment is due (YYYY-MM-DD)
        payment_term: One of upon receipt, net 15, net 30, net 45, net 60, or custom
        tax: First tax rate as a percentage
        tax2: Second tax rate as a percentage
        discount: Discount as a percentage
        estimate_id: Associate the invoice with this estimate
        retainer_id: Associate the invoice with this retainer
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {}
    for key, value in (
        ("client_id", client_id),
        ("subject", subject),
        ("notes", notes),
        ("number", number),
        ("purchase_order", purchase_order),
        ("currency", currency),
        ("issue_date", issue_date),
        ("due_date", due_date),
        ("payment_term", payment_term),
        ("tax", tax),
        ("tax2", tax2),
        ("discount", discount),
        ("estimate_id", estimate_id),
        ("retainer_id", retainer_id),
    ):
        if value is not None:
            params[key] = value

    if not params:
        return json.dumps(
            {"error": "no_fields", "message": "Pass at least one field to update."},
            indent=2,
        )

    response = await harvest_request(f"invoices/{invoice_id}", params, method="PATCH")
    return json.dumps(response, indent=2)


@mcp.tool()
async def add_invoice_line_items(invoice_id: int, line_items: list):
    """Add one or more line items to an existing invoice.

    Args:
        invoice_id: The ID of the invoice
        line_items: List of line-item objects, each with kind, description, unit_price,
            quantity, and optionally taxed / taxed2 / project_id
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    try:
        payload = _line_items_payload(line_items)
    except ValueError as exc:
        return json.dumps({"error": "invalid_line_items", "message": str(exc)}, indent=2)

    response = await harvest_request(
        f"invoices/{invoice_id}", {"line_items": payload}, method="PATCH"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def update_invoice_line_item(
    invoice_id: int,
    line_item_id: int,
    kind: str = None,
    description: str = None,
    unit_price: float = None,
    quantity: float = None,
    taxed: bool = None,
    taxed2: bool = None,
    project_id: int = None,
):
    """Update one line item on an invoice. Only the fields you pass are changed.

    Args:
        invoice_id: The ID of the invoice
        line_item_id: The ID of the line item, from get_invoice
        kind: The line-item category name, e.g. Service or Product
        description: Text shown for this line
        unit_price: Price per unit
        quantity: Number of units
        taxed: Whether the first tax rate applies
        taxed2: Whether the second tax rate applies
        project_id: Associate this line with a project
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    item = {"id": line_item_id}
    for key, value in (
        ("kind", kind),
        ("description", description),
        ("unit_price", unit_price),
        ("quantity", quantity),
        ("taxed", taxed),
        ("taxed2", taxed2),
        ("project_id", project_id),
    ):
        if value is not None:
            item[key] = value

    if len(item) == 1:
        return json.dumps(
            {"error": "no_fields", "message": "Pass at least one field to update."},
            indent=2,
        )

    response = await harvest_request(
        f"invoices/{invoice_id}", {"line_items": [item]}, method="PATCH"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def delete_invoice_line_item(invoice_id: int, line_item_id: int):
    """Remove one line item from an invoice.

    Args:
        invoice_id: The ID of the invoice
        line_item_id: The ID of the line item to remove, from get_invoice
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"invoices/{invoice_id}",
        {"line_items": [{"id": line_item_id, "_destroy": True}]},
        method="PATCH",
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def delete_invoice(invoice_id: int):
    """Delete an invoice permanently. Only draft and open invoices can be deleted.

    Args:
        invoice_id: The ID of the invoice to delete
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(f"invoices/{invoice_id}", method="DELETE")
    return json.dumps(response, indent=2)


@mcp.tool()
async def mark_invoice_closed(invoice_id: int):
    """Mark an open invoice as closed, e.g. written off. Emails no one.

    The invoice must currently be open; Harvest returns 422 otherwise.

    Args:
        invoice_id: The ID of the invoice to close
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"invoices/{invoice_id}/messages", {"event_type": "close"}, method="POST"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def reopen_invoice(invoice_id: int):
    """Re-open a closed invoice. Emails no one.

    The invoice must currently be closed; Harvest returns 422 otherwise.

    Args:
        invoice_id: The ID of the invoice to re-open
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"invoices/{invoice_id}/messages", {"event_type": "re-open"}, method="POST"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def mark_invoice_draft(invoice_id: int):
    """Return an open invoice to draft state. Emails no one.

    The invoice must currently be open; calling this on a draft returns 422
    ("Event type can't be draft unless invoice is open").

    Args:
        invoice_id: The ID of the invoice to return to draft
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"invoices/{invoice_id}/messages", {"event_type": "draft"}, method="POST"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def send_invoice(invoice_id: int, confirm_invoice_number: str):
    """SEND an invoice to its client by email, marking a draft invoice as sent.

    THIS EMAILS A REAL CLIENT and cannot be undone. It is gated twice:
      1. The server must run with HARVEST_ALLOW_INVOICE_SEND=true.
      2. confirm_invoice_number must match the invoice's actual number, which this
         tool re-reads from Harvest before sending.
    Confirm with the user which invoice is going out before calling this.

    Args:
        invoice_id: The ID of the invoice to send
        confirm_invoice_number: The invoice's number, echoed back as confirmation
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    if not HARVEST_ALLOW_INVOICE_SEND:
        return SEND_DISABLED_MESSAGE

    invoice = await harvest_request(f"invoices/{invoice_id}")
    actual_number = str(invoice.get("number") or "")

    if str(confirm_invoice_number).strip() != actual_number:
        return json.dumps(
            {
                "error": "confirmation_mismatch",
                "message": (
                    "Not sending. The confirmation did not match this invoice's number. "
                    "Re-read the invoice and confirm with the user which one to send."
                ),
                "invoice_id": invoice_id,
                "expected_number": actual_number,
                "received": str(confirm_invoice_number),
                "client": (invoice.get("client") or {}).get("name"),
                "amount": invoice.get("amount"),
            },
            indent=2,
        )

    response = await harvest_request(
        f"invoices/{invoice_id}/messages", {"event_type": "send"}, method="POST"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def create_invoice_message(
    invoice_id: int,
    recipients: list,
    confirm_invoice_number: str,
    subject: str = None,
    body: str = None,
    include_link_to_client_invoice: bool = None,
    attach_pdf: bool = None,
    send_me_a_copy: bool = None,
    thank_you: bool = None,
):
    """EMAIL a message about an invoice to named recipients, e.g. a payment reminder.

    THIS SENDS REAL EMAIL and cannot be undone. Gated identically to send_invoice:
    HARVEST_ALLOW_INVOICE_SEND must be true, and confirm_invoice_number must match
    the invoice's actual number.

    Args:
        invoice_id: The ID of the invoice
        recipients: List of recipient objects, each with email and optionally name.
            Example: [{"name": "Gary C.", "email": "gary@example.com"}]
        confirm_invoice_number: The invoice's number, echoed back as confirmation
        subject: Subject line of the email
        body: Body of the email
        include_link_to_client_invoice: Include a link to the client-facing invoice
        attach_pdf: Attach the invoice as a PDF
        send_me_a_copy: Send a copy to the authenticated user
        thank_you: Mark this message as a thank-you note
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    if not HARVEST_ALLOW_INVOICE_SEND:
        return SEND_DISABLED_MESSAGE

    if not isinstance(recipients, list) or not recipients:
        return json.dumps(
            {"error": "invalid_recipients", "message": "recipients must be a non-empty list"},
            indent=2,
        )

    invoice = await harvest_request(f"invoices/{invoice_id}")
    actual_number = str(invoice.get("number") or "")

    if str(confirm_invoice_number).strip() != actual_number:
        return json.dumps(
            {
                "error": "confirmation_mismatch",
                "message": "Not sending. The confirmation did not match this invoice's number.",
                "invoice_id": invoice_id,
                "expected_number": actual_number,
                "received": str(confirm_invoice_number),
            },
            indent=2,
        )

    params = {"recipients": recipients}
    for key, value in (
        ("subject", subject),
        ("body", body),
        ("include_link_to_client_invoice", include_link_to_client_invoice),
        ("attach_pdf", attach_pdf),
        ("send_me_a_copy", send_me_a_copy),
        ("thank_you", thank_you),
    ):
        if value is not None:
            params[key] = value

    response = await harvest_request(
        f"invoices/{invoice_id}/messages", params, method="POST"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def delete_invoice_message(invoice_id: int, message_id: int):
    """Delete an invoice message record. Does not un-send an email already delivered.

    Args:
        invoice_id: The ID of the invoice
        message_id: The ID of the message to delete
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"invoices/{invoice_id}/messages/{message_id}", method="DELETE"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def create_invoice_item_category(name: str):
    """Create an invoice item category.

    Args:
        name: The name of the category, e.g. Service or Product
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        "invoice_item_categories", {"name": name}, method="POST"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def update_invoice_item_category(invoice_item_category_id: int, name: str):
    """Rename an invoice item category.

    Args:
        invoice_item_category_id: The ID of the category to update
        name: The new name
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"invoice_item_categories/{invoice_item_category_id}",
        {"name": name},
        method="PATCH",
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def delete_invoice_item_category(invoice_item_category_id: int):
    """Delete an invoice item category.

    Args:
        invoice_item_category_id: The ID of the category to delete
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(
        f"invoice_item_categories/{invoice_item_category_id}", method="DELETE"
    )
    return json.dumps(response, indent=2)


# --- Reports -------------------------------------------------------------
#
# Harvest exposes ten report endpoints that differ only by grouping, so they are
# folded into four tools rather than ten near-identical ones. Reports aggregate
# server-side: one call answers "hours by project last month" without paging
# thousands of time entries.

TIME_REPORT_GROUPS = ("clients", "projects", "tasks", "team")
EXPENSE_REPORT_GROUPS = ("clients", "projects", "categories", "team")


def _report_params(from_date, to_date, page, per_page):
    params = {"from": from_date, "to": to_date}
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)
    return params


@mcp.tool()
async def get_time_report(
    group_by: str,
    from_date: str,
    to_date: str,
    page: int = None,
    per_page: int = None,
):
    """Summarize tracked hours over a date range, grouped by client, project, task, or team member.

    Returns totals per group (total_hours, billable_hours, billable_amount, currency)
    rather than individual entries. Prefer this over list_time_entries for any
    "how many hours on X" question — it aggregates server-side.

    Args:
        group_by: One of clients, projects, tasks, team
        from_date: Start of the range, inclusive (YYYY-MM-DD or YYYYMMDD)
        to_date: End of the range, inclusive (YYYY-MM-DD or YYYYMMDD)
        page: The page number for pagination
        per_page: The number of records to return per page (1-2000)
    """
    if group_by not in TIME_REPORT_GROUPS:
        return json.dumps(
            {"error": "invalid_group_by", "valid_values": list(TIME_REPORT_GROUPS)}, indent=2
        )

    response = await harvest_request(
        f"reports/time/{group_by}", _report_params(from_date, to_date, page, per_page)
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_expense_report(
    group_by: str,
    from_date: str,
    to_date: str,
    page: int = None,
    per_page: int = None,
):
    """Summarize expenses over a date range, grouped by client, project, category, or team member.

    Requires administrator or manager permissions; regular members get
    "Not authorized!" from the Harvest API.

    Args:
        group_by: One of clients, projects, categories, team
        from_date: Start of the range, inclusive (YYYY-MM-DD or YYYYMMDD)
        to_date: End of the range, inclusive (YYYY-MM-DD or YYYYMMDD)
        page: The page number for pagination
        per_page: The number of records to return per page (1-2000)
    """
    if group_by not in EXPENSE_REPORT_GROUPS:
        return json.dumps(
            {"error": "invalid_group_by", "valid_values": list(EXPENSE_REPORT_GROUPS)}, indent=2
        )

    response = await harvest_request(
        f"reports/expenses/{group_by}", _report_params(from_date, to_date, page, per_page)
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_uninvoiced_report(
    from_date: str,
    to_date: str,
    page: int = None,
    per_page: int = None,
):
    """Report uninvoiced hours and expenses per project over a date range.

    Shows what has been tracked but not yet billed. Requires administrator or
    manager permissions.

    Args:
        from_date: Start of the range, inclusive (YYYY-MM-DD or YYYYMMDD)
        to_date: End of the range, inclusive (YYYY-MM-DD or YYYYMMDD)
        page: The page number for pagination
        per_page: The number of records to return per page (1-2000)
    """
    response = await harvest_request(
        "reports/uninvoiced", _report_params(from_date, to_date, page, per_page)
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_project_budget_report(
    is_active: bool = None, page: int = None, per_page: int = None
):
    """Report budget and budget-spent per project.

    Takes no date range — budgets are reported against the project's own period.

    Args:
        is_active: Pass true for active projects only, false for inactive
        page: The page number for pagination
        per_page: The number of records to return per page (1-2000)
    """
    params = {}
    if is_active is not None:
        params["is_active"] = "true" if is_active else "false"
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)

    response = await harvest_request("reports/project_budget", params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def list_estimates(
    client_id: int = None,
    updated_since: str = None,
    from_date: str = None,
    to_date: str = None,
    state: str = None,
    page: int = None,
    per_page: int = None,
    include_line_items: bool = False,
):
    """List estimates with optional filtering.

    By default the `line_items` array is stripped from each estimate in the
    response to keep list calls within MCP tool-response size limits
    (line_items can account for ~70% of an estimate's payload size). This
    mirrors the summary-vs-detail split that most REST APIs use for list
    endpoints. To fetch full line_items for one estimate, use
    get_estimate_details. To include line_items in every estimate of this
    list call, pass include_line_items=True — but note that large result
    sets can then exceed MCP tool-response size limits.

    Args:
        client_id: Only return estimates belonging to the client with the given ID
        updated_since: Only return estimates updated since the given datetime (e.g. 2021-04-09T12:48:29Z)
        from_date: Only return estimates with an issue_date on or after the given date (YYYY-MM-DD)
        to_date: Only return estimates with an issue_date on or before the given date (YYYY-MM-DD)
        state: Only return estimates with a matching state. One of: draft, sent, accepted, declined
        page: The page number to use in pagination (default: 1)
        per_page: The number of records to return per page (1-2000, default: 25;
            Harvest's native default is 2000 but this wrapper uses 25 since the
            typical summary-mode response per estimate is ~2KB)
        include_line_items: If True, include each estimate's line_items array
            in the response. Defaults to False. Be cautious combining this with
            a high per_page — full estimate payloads are much larger and can
            exceed MCP tool-response size limits.
    """
    params = {}
    if client_id is not None:
        params["client_id"] = str(client_id)
    if updated_since is not None:
        params["updated_since"] = updated_since
    if from_date is not None:
        params["from"] = from_date
    if to_date is not None:
        params["to"] = to_date
    if state is not None:
        params["state"] = state
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)
    else:
        params["per_page"] = "25"

    response = await harvest_request("estimates", params)

    if not include_line_items:
        for est in response.get("estimates", []):
            est.pop("line_items", None)

    return json.dumps(response, indent=2)


@mcp.tool()
async def get_estimate_details(estimate_id: int):
    """Retrieve details for a specific estimate.

    Args:
        estimate_id: The internal integer ID of the estimate (e.g. 4019251),
            NOT the user-facing number ("79") shown in the Harvest UI.
            Use get_estimate_by_number if you only have the number.
    """
    response = await harvest_request(f"estimates/{estimate_id}")
    return json.dumps(response, indent=2)


@mcp.tool()
async def get_estimate_by_number(number: str | int):
    """Retrieve an estimate by its human-readable number (e.g. "79").

    The Harvest API has no direct "get by number" endpoint, so this tool
    lists estimates internally and filters client-side. For better
    performance when you already know the estimate's internal id, prefer
    get_estimate_details.

    Args:
        number: The user-facing estimate number as shown in the Harvest UI
            (e.g. "79", "1001"). Accepts a string or integer.
    """
    number_str = str(number)
    page = 1
    while True:
        response = await harvest_request(
            "estimates",
            {"page": str(page), "per_page": "2000"},
        )
        for est in response.get("estimates", []):
            if est.get("number") == number_str:
                return json.dumps(est, indent=2)
        if not response.get("next_page"):
            break
        page += 1
    raise Exception(f"No estimate found with number {number_str}")


@mcp.tool()
async def list_estimate_messages(
    estimate_id: int,
    updated_since: str = None,
    page: int = None,
    per_page: int = None,
):
    """List messages associated with an estimate.

    Messages are returned sorted by creation date, most recent first.

    Args:
        estimate_id: The internal integer ID of the estimate (e.g. 4019251),
            NOT the user-facing number ("79"). Use get_estimate_by_number
            if you only have the number.
        updated_since: Only return messages updated since the given datetime (e.g. 2021-04-09T12:48:29Z)
        page: The page number for pagination (default: 1). Deprecated by Harvest
            in favor of cursor-based pagination via the response's links.next URL.
        per_page: The number of records to return per page (1-2000, default: 2000)
    """
    params = {}
    if updated_since is not None:
        params["updated_since"] = updated_since
    if page is not None:
        params["page"] = str(page)
    if per_page is not None:
        params["per_page"] = str(per_page)

    response = await harvest_request(f"estimates/{estimate_id}/messages", params)
    return json.dumps(response, indent=2)


@mcp.tool()
async def create_estimate(
    client_id: int,
    number: str = None,
    purchase_order: str = None,
    tax: float = None,
    tax2: float = None,
    discount: float = None,
    subject: str = None,
    notes: str = None,
    currency: str = None,
    issue_date: str = None,
    line_items: list[dict] = None,
):
    """Create a new estimate.

    Args:
        client_id: The ID of the client this estimate belongs to (required)
        number: Estimate number. If omitted, Harvest auto-generates one
        purchase_order: The purchase order number
        tax: Tax percentage applied to the subtotal (e.g. 10.0 for 10%)
        tax2: Second tax percentage applied to the subtotal
        discount: Discount percentage subtracted from the subtotal
        subject: The estimate subject
        notes: Any additional notes to include on the estimate
        currency: Currency code (e.g. "CHF", "EUR", "USD"). Defaults to the client's currency
        issue_date: Date the estimate was issued (YYYY-MM-DD). Defaults to today
        line_items: Array of line item objects. Each item supports:
            - kind (string, required): Estimate item category name (e.g. "Service", "Product")
            - description (string, optional): Text description of the line item
            - quantity (number, optional, defaults to 1): Unit quantity. Harvest's
              docs state integer but decimals (e.g. 0.25, 0.75) are accepted in practice.
            - unit_price (decimal, required): Individual price per unit
            - taxed (boolean, optional, defaults to false): Whether tax applies
            - taxed2 (boolean, optional, defaults to false): Whether tax2 applies
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {"client_id": client_id}
    if number is not None:
        params["number"] = number
    if purchase_order is not None:
        params["purchase_order"] = purchase_order
    if tax is not None:
        params["tax"] = tax
    if tax2 is not None:
        params["tax2"] = tax2
    if discount is not None:
        params["discount"] = discount
    if subject is not None:
        params["subject"] = subject
    if notes is not None:
        params["notes"] = notes
    if currency is not None:
        params["currency"] = currency
    if issue_date is not None:
        params["issue_date"] = issue_date
    if line_items is not None:
        params["line_items"] = line_items

    response = await harvest_request("estimates", params, method="POST")
    return json.dumps(response, indent=2)


@mcp.tool()
async def update_estimate(
    estimate_id: int,
    client_id: int = None,
    number: str = None,
    purchase_order: str = None,
    tax: float = None,
    tax2: float = None,
    discount: float = None,
    subject: str = None,
    notes: str = None,
    currency: str = None,
    issue_date: str = None,
    line_items: list[dict] = None,
):
    """Update an existing estimate.

    Only the parameters you provide are changed; omitted parameters
    remain untouched.

    Args:
        estimate_id: The internal integer ID of the estimate to update
            (e.g. 4019251), NOT the user-facing number ("79"). Use
            get_estimate_by_number if you only have the number.
        client_id: The ID of the client this estimate belongs to
        number: Estimate number
        purchase_order: The purchase order number
        tax: Tax percentage applied to the subtotal (e.g. 10.0 for 10%)
        tax2: Second tax percentage applied to the subtotal
        discount: Discount percentage subtracted from the subtotal
        subject: The estimate subject
        notes: Any additional notes to include on the estimate
        currency: Currency code (e.g. "CHF", "EUR", "USD")
        issue_date: Date the estimate was issued (YYYY-MM-DD)
        line_items: Array of line item objects. To modify the estimate's
            line items:
            - Add a new item: include an object with kind/description/
              quantity/unit_price/taxed/taxed2 (no "id")
            - Update an existing item: include the item's id plus the
              fields to change
            - Delete an existing item: include the item's id and set
              "_destroy": true
            Items not referenced in the request are left untouched.
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {}
    if client_id is not None:
        params["client_id"] = client_id
    if number is not None:
        params["number"] = number
    if purchase_order is not None:
        params["purchase_order"] = purchase_order
    if tax is not None:
        params["tax"] = tax
    if tax2 is not None:
        params["tax2"] = tax2
    if discount is not None:
        params["discount"] = discount
    if subject is not None:
        params["subject"] = subject
    if notes is not None:
        params["notes"] = notes
    if currency is not None:
        params["currency"] = currency
    if issue_date is not None:
        params["issue_date"] = issue_date
    if line_items is not None:
        params["line_items"] = line_items

    response = await harvest_request(f"estimates/{estimate_id}", params, method="PATCH")
    return json.dumps(response, indent=2)


@mcp.tool()
async def change_estimate_state(estimate_id: int, event_type: str):
    """Change the state of an estimate by creating a state-transition message.

    This does not email the estimate — it only changes its state. To actually
    email the estimate to recipients, use send_estimate_message instead.

    Args:
        estimate_id: The internal integer ID of the estimate to transition
            (e.g. 4019251), NOT the user-facing number ("79"). Use
            get_estimate_by_number if you only have the number.
        event_type: One of:
            - "send": mark a draft estimate as sent
            - "accept": mark a sent estimate as accepted (closes it)
            - "decline": mark a sent estimate as declined (closes it)
            - "re-open": reopen a closed (accepted/declined) estimate back to sent
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {"event_type": event_type}
    response = await harvest_request(
        f"estimates/{estimate_id}/messages", params, method="POST"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def send_estimate_message(
    estimate_id: int,
    recipients: list[dict],
    subject: str = None,
    body: str = None,
    send_me_a_copy: bool = None,
    event_type: str = None,
):
    """Create an estimate message. **This sends an email to the recipients.**

    Use this to email an estimate to a client. To merely change the estimate's
    state without sending email (e.g. "mark as sent"), use change_estimate_state
    instead.

    Note: If the estimate is in "draft" state, sending any message with
    recipients will automatically transition it to "sent" (even without
    passing event_type). This matches Harvest's UI behavior — emailing
    implies sending.

    Args:
        estimate_id: The internal integer ID of the estimate to send a
            message for (e.g. 4019251), NOT the user-facing number ("79").
            Use get_estimate_by_number if you only have the number.
        recipients: Array of recipient objects (required). Each must have:
            - email (string, required): Email address of the recipient
            - name (string, optional): Display name of the recipient
            Example: [{"name": "Jane Doe", "email": "jane@example.com"}]
        subject: The message subject
        body: The message body
        send_me_a_copy: If true, a copy of the email is sent to the current user (default false)
        event_type: Optionally also run a state transition alongside the email.
            One of: "send", "accept", "decline", "re-open".
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    params = {"recipients": recipients}
    if subject is not None:
        params["subject"] = subject
    if body is not None:
        params["body"] = body
    if send_me_a_copy is not None:
        params["send_me_a_copy"] = send_me_a_copy
    if event_type is not None:
        params["event_type"] = event_type

    response = await harvest_request(
        f"estimates/{estimate_id}/messages", params, method="POST"
    )
    return json.dumps(response, indent=2)


@mcp.tool()
async def delete_estimate(estimate_id: int):
    """Delete an estimate.

    Args:
        estimate_id: The internal integer ID of the estimate to delete
            (e.g. 4019251), NOT the user-facing number ("79"). Use
            get_estimate_by_number if you only have the number.
    """
    if HARVEST_READ_ONLY:
        return READ_ONLY_MESSAGE

    response = await harvest_request(f"estimates/{estimate_id}", method="DELETE")
    return json.dumps(response, indent=2)


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport="stdio")
