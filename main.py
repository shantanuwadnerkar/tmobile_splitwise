#!/usr/bin/env python3
"""
T-Mobile Bill to Splitwise Automation

Main entry point that orchestrates:
1. Downloading the bill from T-Mobile
2. Parsing the bill with Gemini
3. Posting charges to Splitwise
"""

from splitwise_client import create_tmobile_expense_note
import traceback
import argparse
import os
import sys
from pathlib import Path

import yaml

from utils import init_env
from tmobile_scraper import download_bill
from bill_parser import parse_bill
from splitwise_client import create_splitwise_expense

CONFIG_PATH = "config.yaml"


def load_config(config_path: str) -> dict:
    """Load configuration from YAML file."""
    config_file = Path(config_path)
    if not config_file.exists():
        print(f"Error: Config file not found: {config_path}")
        print("Please copy config.yaml.example to config.yaml and fill in your credentials.")
        sys.exit(1)

    with open(config_file, "r") as f:
        return yaml.safe_load(f)


def find_latest_bill(config: dict) -> Path:
    """Find the most recent bill PDF in the configured bills directory."""
    bills_dir = Path(config["output"].get("bills_directory"))
    if not bills_dir.exists():
        print("Error: --skip-download requires either --pdf-path or existing bills in bills directory")
        sys.exit(1)
    pdf_files = sorted(bills_dir.glob("*.pdf"), key=os.path.getmtime, reverse=True)
    if not pdf_files:
        print(f"Error: No PDF files found in {bills_dir}")
        sys.exit(1)
    return pdf_files[0]


def run_full_workflow(config: dict, skip_download: bool, pdf_path: Path | None, dry_run: bool, use_cache: bool, prompt_version: int | None = None):
    """
    Run the complete workflow: download bill, parse it, and post to Splitwise.

    Args:
        config: Configuration dictionary
        skip_download: If True, skip downloading and use existing PDF
        pdf_path: Path to existing PDF (required if skip_download is True)
        dry_run: If True, don't actually create the Splitwise expense
        use_cache: If True, cache LLM responses for faster re-runs
        prompt_version: Prompt version to use (None = latest)
    """
    print("=" * 60)
    print("T-Mobile Bill to Splitwise Automation")
    print("=" * 60)

    # Step 1: Download bill (or use existing)
    if not skip_download:
        print("\n[Step 1/3] Downloading bill from T-Mobile...")
        try:
            pdf_path = download_bill(config)
            print(f"Bill downloaded: {pdf_path}")
        except Exception as e:
            print(f"Error downloading bill: {traceback.format_exc()}")
            sys.exit(1)
    else:
        if pdf_path:
            pdf_path = Path(pdf_path)
            print(f"\nUsing provided PDF: {pdf_path}")
        else:
            pdf_path = find_latest_bill(config)
            print(f"\nUsing most recent bill: {pdf_path}")

    # Step 2: Parse bill with Gemini
    print("\n[Step 2/3] Parsing bill with Gemini...")
    result = parse_bill(config, pdf_path, use_cache=use_cache, prompt_version=prompt_version)
    bill_summary = result.summary
    print(f"Bill parsed successfully!")
    print(f"  Billing Period: {bill_summary.billing_period}")
    print(f"  Total Charge: ${bill_summary.total_charge:.2f}")
    print(f"  Phone Lines: {len(bill_summary.phone_lines)}, Watch Lines: {len(bill_summary.watch_lines)}")

    # Display splits
    print("\nSplit breakdown:")
    for user_id, split in result.splits.items():
        print(f"  {split.name}: ${split.amount:.2f}")
        print(f"    Lines: {', '.join(split.lines)}")

    # Step 3: Post to Splitwise
    print("\n[Step 3/3] Posting to Splitwise...")
    if not dry_run:
        create_splitwise_expense(config, result)
        print(f"Expense created successfully!")
    else:
        note = create_tmobile_expense_note(result)
        print("DRY RUN - Expense would be created with:")
        print(f"  Description: T-Mobile Bill - {bill_summary.billing_period}")
        print(f"  Total: ${bill_summary.total_charge:.2f}")
        print(f"  Group ID: {config['splitwise']['group_name']}")
        print(f"  Paid by User ID: {config['line_mapping']['paid_by']}")
        print(f"  Note: {note}")

    print("\n" + "=" * 60)
    print("Workflow completed successfully!")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="T-Mobile Bill to Splitwise Automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run full workflow (download, parse, post)
  python main.py

  # Dry run (don't actually post to Splitwise)
  python main.py --dry-run

  # Use existing PDF instead of downloading
  python main.py --skip-download --pdf-path ./bills/tmobile_bill_2024-01.pdf

  # Parse a bill without posting, with LLM response caching
  python main.py --skip-download --dry-run --cache --pdf-path ./bills/tmobile_bill_2024-01.pdf
        """
    )


    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip downloading bill, use existing PDF"
    )

    parser.add_argument(
        "--pdf-path",
        help="Path to existing PDF file (used with --skip-download)"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse bill but don't create Splitwise expense"
    )

    parser.add_argument(
        "--cache",
        action="store_true",
        help="Cache LLM responses for faster re-runs"
    )

    parser.add_argument(
        "--prompt-version",
        type=int,
        default=None,
        help="Prompt version to use (default: latest)"
    )

    args = parser.parse_args()

    # Change to script directory for relative paths
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    # Load .env and validate all required env vars
    init_env()

    # Load config
    config = load_config(CONFIG_PATH)

    run_full_workflow(
        config=config,
        skip_download=args.skip_download,
        pdf_path=args.pdf_path,
        dry_run=args.dry_run,
        use_cache=args.cache,
        prompt_version=args.prompt_version,
    )


if __name__ == "__main__":
    main()
