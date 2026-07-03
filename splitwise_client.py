"""
Splitwise API Client

Handles authentication and expense creation for splitting T-Mobile bills.
"""

import os
from datetime import datetime
from typing import Optional
from splitwise import Splitwise
from splitwise.expense import Expense
from splitwise.user import ExpenseUser

from utils import get_env, init_env, SPLITWISE_ENV_VARS


class SplitwiseClient:
    """Client for interacting with Splitwise API."""

    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        api_key: Optional[str] = None
    ):
        """
        Initialize the Splitwise client.

        Args:
            consumer_key: Splitwise OAuth consumer key
            consumer_secret: Splitwise OAuth consumer secret
            api_key: Splitwise API key (alternative to OAuth)
        """
        if api_key:
            # Use API key authentication (simpler, no OAuth flow needed)
            self.sw = Splitwise(consumer_key, consumer_secret, api_key=api_key)
        else:
            # Use OAuth (will require browser authentication on first run)
            self.sw = Splitwise(consumer_key, consumer_secret)

        self.current_user = None

    def get_current_user(self):
        """Get the currently authenticated user."""
        if self.current_user is None:
            self.current_user = self.sw.getCurrentUser()
        return self.current_user

    def get_groups(self):
        """Get all groups the user is a member of."""
        return self.sw.getGroups()

    def get_group(self, group_name: str):
        """Get a specific group by ID."""
        return self.sw.getGroup(group_name)

    def get_group_members(self, group_name: str) -> dict:
        """
        Get all members of a group.

        Args:
            group_name: The Splitwise group ID

        Returns:
            Dictionary mapping user ID to user info
        """
        group = self.get_group(group_name)
        members = {}
        for member in group.getMembers():
            members[member.getId()] = {
                "id": member.getId(),
                "first_name": member.getFirstName(),
                "last_name": member.getLastName(),
                "email": member.getEmail()
            }
        return members

    def create_expense(
        self,
        group_id: int,
        description: str,
        total_amount: float,
        user_shares: dict,
        paid_by_user_id: int,
        date: Optional[datetime] = None,
        notes: Optional[str] = None
    ) -> Expense:
        """
        Create an expense in Splitwise with custom shares.

        Args:
            group_id: The Splitwise group ID
            description: Expense description (e.g., "T-Mobile Bill - January 2024")
            total_amount: Total expense amount
            user_shares: Dictionary mapping user_id to amount owed
            paid_by_user_id: User ID of who paid the bill
            date: Expense date (defaults to today)
            notes: Optional notes for the expense

        Returns:
            Created Expense object
        """
        expense = Expense()
        expense.setGroupId(group_id)
        expense.setDescription(description)
        expense.setCost(str(round(total_amount, 2)))

        if date:
            expense.setDate(date.strftime("%Y-%m-%dT%H:%M:%SZ"))
        else:
            expense.setDate(datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"))

        if notes:
            expense.setDetails(notes)

        # Set up expense users with their shares
        expense_users = []

        for user_id, share_info in user_shares.items():
            user = ExpenseUser()
            user.setId(user_id)

            # Amount this user owes
            amount_owed = share_info.amount if hasattr(share_info, 'amount') else (share_info["amount"] if isinstance(share_info, dict) else share_info)
            user.setOwedShare(str(round(amount_owed, 2)))

            # If this user paid the bill, set their paid share
            if user_id == paid_by_user_id:
                user.setPaidShare(str(round(total_amount, 2)))
            else:
                user.setPaidShare("0")

            expense_users.append(user)

        # Calculate and distribute any difference due to floating point rounding
        _redistribute_expenses(expense_users, total_amount, paid_by_user_id)

        expense.setUsers(expense_users)

        owed_share = 0.0
        for user in expense.getUsers():
            owed_share += float(user.getOwedShare())

        # Create the expense
        created_expense, errors = self.sw.createExpense(expense)

        if errors:
            raise Exception(f"Failed to create expense: {created_expense}, {errors}")

        return created_expense

    def create_tmobile_expense(
        self,
        group_name: str,
        bill_result,
        paid_by_user: str,
    ) -> Expense:
        """
        Create a T-Mobile bill expense with standard formatting.

        Args:
            group_name: The Splitwise group name
            bill_result: BillResult with summary and splits
            paid_by_user: User ID of the account owner who paid

        Returns:
            Created Expense object
        """
        # Resolve group name to ID
        groups = self.get_groups()
        matched_groups = [g for g in groups if g.getName().lower() == group_name.lower()]
        if not matched_groups:
            raise ValueError(f"No group found matching '{group_name}'")
        group = matched_groups[0]
        group_id = group.getId()

        # Resolve paid_by_user to user ID
        matched_users = []
        for u in group.getMembers():
            first = u.getFirstName()
            last = u.getLastName()
            if first is None and last is None:
                continue
            full_name = f"{first or ''} {last or ''}".strip()
            if full_name.lower() == paid_by_user.lower():
                matched_users.append(u)
                
        if not matched_users:
            raise ValueError(f"No user found in group matching '{paid_by_user}'")
        paid_by_user_id = matched_users[0].getId()

        # Use month from paid_on (format: "DD Month YYYY") for the description
        paid_on_parts = bill_result.summary.paid_on.split(" ", 1)
        billing_month = paid_on_parts[1] if len(paid_on_parts) > 1 else bill_result.summary.paid_on
        description = f"T-Mobile Bill - {billing_month}"

        notes = create_tmobile_expense_note(bill_result)

        # Build user_shares as {int(user_id): {"amount": float}} to match
        # the format expected by create_expense (same as add_expense_from_file)
        user_shares = {}
        for user_id, split in bill_result.splits.items():
            user_shares[int(user_id)] = {"amount": split.amount}

        return self.create_expense(
            group_id=group_id,
            description=description,
            total_amount=bill_result.summary.total_charge,
            user_shares=user_shares,
            paid_by_user_id=paid_by_user_id,
            notes=notes
        )


def _redistribute_expenses(expense_users: list, total_amount: float, paid_by_user_id: int):
    """
    Adjust owed shares by a few cents to fix floating point rounding differences.
    """
    owed_share_total = sum(float(u.getOwedShare()) for u in expense_users)
    diff_cents = round((total_amount - owed_share_total) * 100)

    if diff_cents != 0:
        eligible_users = [u for u in expense_users if u.getId() != paid_by_user_id]
        
        if abs(diff_cents) > len(expense_users):
            raise ValueError(
                f"Rounding difference ({diff_cents} cents) is unexpectedly large "
                f"for {len(expense_users)} users."
            )
            
        step = 1 if diff_cents > 0 else -1
        users_to_adjust = eligible_users[:abs(diff_cents)]
        
        for u in users_to_adjust:
            current_owed = float(u.getOwedShare())
            new_owed = current_owed + (step / 100.0)
            u.setOwedShare(str(round(new_owed, 2)))


def create_tmobile_expense_note(bill_result) -> str:
    """
    Build a detailed expense note from a BillResult.

    Includes billing period and per-user charge breakdowns.

    Args:
        bill_result: BillResult with summary and splits

    Returns:
        Formatted note string for the Splitwise expense.
    """
    parts = [f"Billing Period: {bill_result.summary.billing_period}", ""]
    for user_id, split in bill_result.splits.items():
        parts.append(f"{split.name}: ${split.amount:.2f}")
        if split.notes:
            parts.append(split.notes)
        parts.append("")
    return "\n".join(parts).strip()


def create_splitwise_expense(config: dict, bill_result) -> Expense:
    """
    Convenience function to create a Splitwise expense from config and bill data.

    Args:
        config: Configuration dictionary with splitwise section
        bill_result: BillResult with summary and splits

    Returns:
        Created Expense object
    """
    client = SplitwiseClient(
        consumer_key=get_env("SPLITWISE_CONSUMER_KEY"),
        consumer_secret=get_env("SPLITWISE_CONSUMER_SECRET"),
        api_key=os.environ.get("SPLITWISE_API_KEY")
    )

    print(f"{config["line_mapping"]["paid_by"]=}")
    return client.create_tmobile_expense(
        group_name=config["splitwise"]["group_name"],
        bill_result=bill_result,
        paid_by_user=config["line_mapping"]["paid_by"],
    )


def list_groups(config: dict):
    """List all Splitwise groups for the authenticated user."""
    client = SplitwiseClient(
        consumer_key=get_env("SPLITWISE_CONSUMER_KEY"),
        consumer_secret=get_env("SPLITWISE_CONSUMER_SECRET"),
        api_key=os.environ.get("SPLITWISE_API_KEY")
    )

    groups = client.get_groups()
    print("Your Splitwise Groups:")
    print("-" * 50)
    for group in groups:
        print(f"  ID: {group.getId()}, Name: {group.getName()}")
        members = group.getMembers()
        for member in members:
            print(f"    - {member.getFirstName()} {member.getLastName()} (ID: {member.getId()})")
    print("-" * 50)


def list_members(config: dict, group_name: str):
    """List members of a specific Splitwise group by name."""
    client = SplitwiseClient(
        consumer_key=get_env("SPLITWISE_CONSUMER_KEY"),
        consumer_secret=get_env("SPLITWISE_CONSUMER_SECRET"),
        api_key=os.environ.get("SPLITWISE_API_KEY")
    )

    groups = client.get_groups()
    matched = [g for g in groups if g.getName().lower() == group_name.lower()]
    if not matched:
        print(f"No group found matching '{group_name}'")
        print("Available groups:")
        for group in groups:
            print(f"\t{group.getName()} (ID: {group.getId()})")
        return

    for group in matched:
        print(f"Members of '{group.getName()}' (ID: {group.getId()}):")
        print("-" * 50)
        for member in group.getMembers():
            print(f"\t{member.getFirstName()} {member.getLastName()} (ID: {member.getId()})")
        print("-" * 50)


def get_user_id(config: dict, name: str):
    """Find a Splitwise user ID by name across all groups."""
    client = SplitwiseClient(
        consumer_key=get_env("SPLITWISE_CONSUMER_KEY"),
        consumer_secret=get_env("SPLITWISE_CONSUMER_SECRET"),
        api_key=os.environ.get("SPLITWISE_API_KEY")
    )

    groups = client.get_groups()
    matches = []
    for group in groups:
        for member in group.getMembers():
            full_name = f"{member.getFirstName()} {member.getLastName()}"
            if name.lower() in full_name.lower():
                matches.append((member.getId(), full_name, group.getName()))

    if not matches:
        print(f"No user found matching '{name}'")
        return

    # Deduplicate by user ID
    seen = set()
    for user_id, full_name, group_name in matches:
        if user_id not in seen:
            seen.add(user_id)
            print(f"{full_name} (ID: {user_id})")


def add_expense_from_file(config: dict, group_name: str, json_path: str):
    """
    Add an expense to a Splitwise group from a JSON file.

    Expected JSON format:
    {
        "title": "T-Mobile Bill - January 2026",
        "description": "Monthly bill split",
        "total_amount": 350.00,
        "paid_by_user": "Foo",
        "date": "2026-01-15",
        "user_shares": {
            "Foo": {"amount": 100.00},
            "Bar": {"amount": 250.00}
        }
    }
    """
    import json

    # Load expense data from JSON
    with open(json_path, "r") as f:
        data = json.load(f)

    client = SplitwiseClient(
        consumer_key=get_env("SPLITWISE_CONSUMER_KEY"),
        consumer_secret=get_env("SPLITWISE_CONSUMER_SECRET"),
        api_key=os.environ.get("SPLITWISE_API_KEY")
    )

    # Resolve group name to ID and build name -> member ID lookup
    groups = client.get_groups()
    matched = [g for g in groups if g.getName().lower() == group_name.lower()]
    if not matched:
        print(f"No group found matching '{group_name}'")
        print("Available groups:")
        for group in groups:
            print(f"\t{group.getName()} (ID: {group.getId()})")
        return
    group = matched[0]
    group_name = group.getId()

    # Build name -> user ID mapping from group members
    name_to_id = {}
    for member in group.getMembers():
        first = member.getFirstName()
        last = member.getLastName()
        if first is None and last is None:
            continue
        full_name = f"{first or ''} {last or ''}".strip()
        name_to_id[full_name.lower()] = member.getId()

    def resolve_name(name: str) -> int:
        user_id = name_to_id.get(name.lower())
        if user_id is None:
            raise ValueError(f"No group member found matching '{name}'. "
                           f"Available: {', '.join(name_to_id.keys())}")
        return user_id

    # Parse optional date
    expense_date = None
    if "date" in data:
        expense_date = datetime.strptime(data["date"], "%Y-%m-%d")

    # Resolve names to user IDs
    paid_by_user = resolve_name(data["paid_by_user"])
    user_shares = {resolve_name(name): share for name, share in data["user_shares"].items()}

    client.create_expense(
        group_name=group_name,
        description=data["title"],
        total_amount=data["total_amount"],
        user_shares=user_shares,
        paid_by_user=paid_by_user,
        date=expense_date,
        notes=data.get("description"),
    )
    print(f"Expense created: {data['title']} (${data['total_amount']:.2f})")


if __name__ == "__main__":
    import argparse
    import yaml

    init_env(SPLITWISE_ENV_VARS)

    parser = argparse.ArgumentParser(description="Splitwise CLI utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-groups", help="List all groups and member IDs")

    members_parser = subparsers.add_parser("list-members", help="List members of a specific group")
    members_parser.add_argument("--group-name", required=True, help="Name of the Splitwise group")

    user_id_parser = subparsers.add_parser("get-user-id", help="Get user ID by name")
    user_id_parser.add_argument("--name", required=True, help="Name of the user to search for")

    expense_parser = subparsers.add_parser("add-expense", help="Add an expense from a JSON file")
    expense_parser.add_argument("--group-name", required=True, help="Name of the Splitwise group")
    expense_parser.add_argument("--expenses", required=True, help="Path to JSON file with expense details")

    args = parser.parse_args()

    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    if args.command == "list-groups":
        list_groups(config)
    elif args.command == "list-members":
        list_members(config, args.group_name)
    elif args.command == "get-user-id":
        get_user_id(config, args.name)
    elif args.command == "add-expense":
        add_expense_from_file(config, args.group_name, args.expenses)
