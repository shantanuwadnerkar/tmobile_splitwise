"""
T-Mobile Bill Parser using Google Gemini

Extracts per-line charges and shared fees from T-Mobile bill PDFs.
Uses the summary table on page 2 with columns: Line, Total, Plans, Equipment, Services.
"""

import json
import re
from pathlib import Path

import attr
import jsonschema
from google import genai

from utils import get_env, init_env, schema_to_example, GEMINI_ENV_VARS
from prompt_manager import PromptManager

VALIDATION_TOLERANCE = 0.001  # Dollar tolerance for charge validation


@attr.s(auto_attribs=True, kw_only=True)
class PhoneLineCharge:
    """Charges for a single phone line from the bill summary table."""
    phone_number: str
    name: str
    base_charge: float
    equipment: float
    services: float
    one_time_charges: float
    total_charge: float = attr.ib(init=False)

    def __attrs_post_init__(self):
        self.total_charge = self.base_charge + self.equipment + self.services + self.one_time_charges


@attr.s(auto_attribs=True, kw_only=True)
class WatchLineCharge:
    """Charges for a watch line from the bill summary table."""
    phone_number: str
    name: str
    associated_phone_number: str
    base_charge: float
    equipment: float
    services: float
    one_time_charges: float
    total_charge: float = attr.ib(init=False)

    def __attrs_post_init__(self):
        self.total_charge = self.base_charge + self.equipment + self.services + self.one_time_charges


@attr.s(auto_attribs=True, kw_only=True, frozen=True)
class BillSummary:
    """
    Parsed T-Mobile bill summary from the summary table on page 2.
    :billing_period: The month and year of the bill (e.g., "January 2026")
    :total_charge: The total amount of the bill
    :phone_lines: List of phone line charges
    :watch_lines: List of watch line charges
    :base_charge: The phone line base charge which is split equally. Watch lines are excluded from this.
    :equipment: The total equipment charge of the bill
    :services: The total services charge of the bill
    :one_time_charges: The total one-time charges of the bill
    """
    billing_period: str
    paid_on: str
    total_charge: float
    phone_lines: list[PhoneLineCharge]
    watch_lines: list[WatchLineCharge]
    base_charge: float
    equipment: float
    services: float
    one_time_charges: float

    @property
    def total_phone_charges(self) -> float:
        """Sum of all phone line total charges."""
        return sum(line.total_charge for line in self.phone_lines)

    @property
    def total_watch_charges(self) -> float:
        """Sum of all watch line total charges."""
        return sum(line.total_charge for line in self.watch_lines)

    @property
    def all_lines(self):
        """All lines (phone + watch) combined."""
        return self.phone_lines + self.watch_lines

    def validate(self) -> bool:
        """Validate bill summary consistency."""
        # Total charge validation
        watch_base_charge = sum(line.base_charge for line in self.watch_lines)
        expected_total = self.base_charge + self.equipment + self.services + self.one_time_charges + watch_base_charge
        if abs(self.total_charge - expected_total) > VALIDATION_TOLERANCE:
            raise ValueError(f"total_charge (${self.total_charge:.2f}) != "
                  f"base_charge + equipment + services + one_time_charges + watch_base_charge (${expected_total:.2f})")

        # Base charge validation
        phone_base_charge = sum(l.base_charge for l in self.phone_lines)
        if abs(phone_base_charge - self.base_charge) > VALIDATION_TOLERANCE:
            raise ValueError(f"base_charge (${self.base_charge:.2f}) != "
                    f"sum of phone line base charge (${phone_base_charge:.2f})")

        # Equipment should equal sum of all line equipment charges
        lines_equipment = sum(l.equipment for l in self.all_lines)
        if abs(self.equipment - lines_equipment) > VALIDATION_TOLERANCE:
            raise ValueError(f"bill equipment (${self.equipment:.2f}) != "
                  f"sum of line equipment (${lines_equipment:.2f})")

        # Services should equal sum of all line service charges
        lines_services = sum(l.services for l in self.all_lines)
        if abs(self.services - lines_services) > VALIDATION_TOLERANCE:
            raise ValueError(f"bill services (${self.services:.2f}) != "
                  f"sum of line services (${lines_services:.2f})")

        # One-time charges should equal sum of all line one-time charges
        lines_one_time_charges = sum(l.one_time_charges for l in self.all_lines)
        if abs(self.one_time_charges - lines_one_time_charges) > VALIDATION_TOLERANCE:
            raise ValueError(f"bill one_time_charges (${self.one_time_charges:.2f}) != "
                  f"sum of line one_time_charges (${lines_one_time_charges:.2f})")

        return True


@attr.s(auto_attribs=True, kw_only=True, frozen=True)
class UserSplit:
    """Per-user split result from bill calculation."""
    splitwise_user_id: int
    name: str
    amount: float
    lines: list[str]
    notes: str


# Typed mapping: splitwise_user_id -> UserSplit
SplitsMap = dict[int, UserSplit]


@attr.s(auto_attribs=True, kw_only=True, frozen=True)
class BillResult:
    """Complete parsed bill with calculated splits."""
    summary: BillSummary
    splits: SplitsMap


@attr.s(auto_attribs=True, kw_only=True)
class BillParser:
    """
    Parser for T-Mobile bills using Google Gemini.
    Ties to the prompts/bill_extraction.txt file for the LLM prompt.
    """

    PROMPTS_DIR = Path(__file__).parent / "prompts"
    SCHEMAS_DIR = Path(__file__).parent / "schemas"
    CACHE_DIR = Path(__file__).parent / ".bill_cache"

    model: str
    config: dict
    prompt_version: int | None = None

    # Set in __attrs_post_init__
    client: genai.Client = attr.ib(init=False)
    config_by_line: dict = attr.ib(init=False, factory=dict)
    bill_output_schema: dict = attr.ib(init=False)
    extraction_prompt: str = attr.ib(init=False)
    prompt_manager: PromptManager = attr.ib(init=False)

    def __attrs_post_init__(self):
        self.client = genai.Client(api_key=get_env("GEMINI_API_KEY"))

        # Load and process schema
        self.bill_output_schema = json.loads(
            (self.SCHEMAS_DIR / "bill_output.json").read_text()
        )

        # Load prompt via PromptManager
        self.prompt_manager = PromptManager(prompts_dir=self.PROMPTS_DIR)
        prompt_template = self.prompt_manager.get_prompt(self.prompt_version)
        self.extraction_prompt = prompt_template.replace(
            "{{OUTPUT_SCHEMA}}",
            json.dumps(schema_to_example(self.bill_output_schema), indent=4)
        )

        # Build config lookup by normalized phone number
        self._build_config_by_line(self.config.get("line_mapping", {}))

    def _load_cache(self, pdf_path: Path) -> dict | None:
        """Load cached LLM response if it exists."""
        cache_file = self.CACHE_DIR / f"{pdf_path.stem}.json"
        if cache_file.exists():
            print(f"Loading cached LLM response: {cache_file}")
            with open(cache_file, "r") as f:
                return json.load(f)
        return None

    def _save_cache(self, pdf_path: Path, data: dict) -> None:
        """Save LLM response to cache."""
        self.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = self.CACHE_DIR / f"{pdf_path.stem}.json"
        with open(cache_file, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Cached LLM response: {cache_file}")

    def _build_config_by_line(self, line_mapping: dict) -> None:
        """Build watch line to phone line mapping."""
        # Build config lookup by normalized phone number
        for line in line_mapping.get("lines", []):
            phone_norm = re.sub(r"\D", "", line["phone_number"])
            self.config_by_line[phone_norm] = line

    def _get_line_type(self, phone_number: str) -> str:
        """Get the line type ('phone' or 'watch') for a given phone number."""
        phone_norm = re.sub(r"\D", "", phone_number)
        return self.config_by_line.get(phone_norm, {}).get("type", "phone")

    def _is_watch_line(self, phone_number: str) -> bool:
        """Check if a phone number belongs to a watch line."""
        return self._get_line_type(phone_number) == "watch"

    def _is_phone_line(self, phone_number: str) -> bool:
        """Check if a phone number belongs to a phone line."""
        return self._get_line_type(phone_number) == "phone"

    def parse_bill(self, pdf_path: Path, use_cache: bool) -> BillSummary:
        """
        Parse a T-Mobile bill PDF and extract billing information.

        Args:
            pdf_path: Path to the PDF file
            use_cache: If True, cache LLM responses and reuse for same PDFs

        Returns:
            BillSummary object with extracted billing data
        """
        data = None
        line_mapping = self.config.get("line_mapping")
        self._build_config_by_line(line_mapping)

        # Try loading from cache
        if use_cache:
            data = self._load_cache(pdf_path=pdf_path)

        # Call Gemini if no cached data
        if data is None:
            data = self._call_gemini(pdf_path)
            if use_cache:
                self._save_cache(pdf_path=pdf_path, data=data)

        self._validate_llm_output(data=data)

        # Convert to BillSummary
        bill_summary = self._convert_to_bill_summary(data, line_mapping)
        bill_summary.validate()

        # Calculate splits and return BillResult
        splits = self._calculate_splits(bill_summary)
        return BillResult(summary=bill_summary, splits=splits)

    def _validate_llm_output(self, data: dict) -> None:
        """Validate LLM output against JSON schema and arithmetic constraints."""
        print("Validating LLM output...")

        # Schema validation
        jsonschema.validate(instance=data, schema=self.bill_output_schema)

        # Ensures total charge is the amount paid. Ensure all numbers add up to the total charge. Otherwise there's an error.
        total_charge = float(data.get("total_charge", 0.0))
        base_charge = float(data.get("base_charge", 0.0))
        equipment_charge = float(data.get("equipment", 0.0))
        services_charge = float(data.get("services", 0.0))
        one_time_charges = float(data.get("one_time_charges", 0.0))
        expected_total_charge = base_charge + equipment_charge + services_charge + one_time_charges
        if abs(total_charge - expected_total_charge) > VALIDATION_TOLERANCE:
            raise ValueError(f"Total charge (${total_charge:.2f}) != "
                  f"sum of line charges (${expected_total_charge:.2f}). Base charge: ${base_charge:.2f}, "
                  f"equipment charge: ${equipment_charge:.2f}, services charge: ${services_charge:.2f}, "
                  f"one-time charges: ${one_time_charges:.2f}")

        # Some more table validation
        watch_line_base_charge = 0.0
        line_equipment_charge = 0.0
        line_services_charge = 0.0
        line_one_time_charges = 0.0
        for line in data.get("lines", []):
            phone_norm = re.sub(r"\D", "", line.get("phone_number", ""))
            if self._is_watch_line(phone_norm):
                watch_line_base_charge += line.get("base_charge", 0.0)
            line_equipment_charge += line.get("equipment", 0.0)
            line_services_charge += line.get("services", 0.0)
            line_one_time_charges += line.get("one_time_charges", 0.0)

        # This removes account-wide Netflix charge from the total charge
        if services_charge > 0:
            total_charge -= services_charge

        phone_line_base_charge = base_charge - watch_line_base_charge
        calculated_total_amount = phone_line_base_charge + watch_line_base_charge + line_equipment_charge + line_services_charge + line_one_time_charges
        error_str = f"""
        Total amount does not match the sum of individual line charges.
        Total charge: ${total_charge:.2f}. Calculated total amount: ${calculated_total_amount:.2f}. Base charge: ${base_charge:.2f}, equipment charge: ${equipment_charge:.2f}, services charge: ${services_charge:.2f}, one-time charges: ${one_time_charges:.2f}
        """
        assert abs(calculated_total_amount - total_charge) < VALIDATION_TOLERANCE, error_str

    def _call_gemini(self, pdf_path: Path) -> dict:
        """Upload PDF to Gemini and extract billing data."""
        print(f"Uploading PDF to Gemini: {pdf_path}")

        uploaded_file = self.client.files.upload(file=str(pdf_path), config={'mime_type': 'application/pdf'})
        print(f"File uploaded successfully: {uploaded_file.name}")

        print("Analyzing bill with Gemini...")
        response = self.client.models.generate_content(
            model=self.model,
            contents=[uploaded_file, self.extraction_prompt],
            config=genai.types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=50000,
            )
        )

        response_text = response.text.strip()

        # Clean up response (remove markdown code blocks if present)
        if response_text.startswith("```"):
            response_text = re.sub(r"```json?\s*", "", response_text)
            response_text = re.sub(r"```\s*$", "", response_text)
            response_text = response_text.strip()

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as e:
            raise Exception(f"Failed to parse Gemini response as JSON: {e}\nResponse: {response_text}")

        # Clean up the uploaded file
        try:
            self.client.files.delete(name=uploaded_file.name)
        except Exception as e:
            print(f"Failed to delete uploaded file: {e}")
            pass

        return data

    def _convert_to_bill_summary(self, data: dict, line_mapping: dict) -> BillSummary:
        """
        Function to convert parsed JSON data to BillSummary object.
        The function stores all implicit assumptions with the prompt and converts it to a proper BillSummary object.
        """

        phone_lines = []
        watch_lines = []

        num_phone_lines = 0
        num_watch_lines = 0
        watch_base_charge = 0.0
        for line in data.get("lines", []):
            phone_number = line.get("phone_number", "")
            if self._is_phone_line(phone_number):
                num_phone_lines += 1
            elif self._is_watch_line(phone_number):
                num_watch_lines += 1
                watch_base_charge += float(line.get("base_charge", 0.0))

        base_charge = float(data.get("base_charge", 0.0))
        phone_line_base_charge = base_charge - watch_base_charge

        for line in data.get("lines", []):
            phone_number = line.get("phone_number", "Unknown")
            phone_norm = re.sub(r"\D", "", phone_number)
            cfg = self.config_by_line.get(phone_norm, {})
            line_type = self._get_line_type(phone_number)
            name = cfg.get("name", "Unknown")
            associated_phone_number = cfg.get("associated_phone_number", "")

            if line_type == "phone":
                phone_lines.append(PhoneLineCharge(
                    phone_number=phone_number,
                    name=name,
                    base_charge=phone_line_base_charge/num_phone_lines,
                    equipment=float(line.get("equipment", 0.0)),
                    services=float(line.get("services", 0.0)),
                    one_time_charges=float(line.get("one_time_charges", 0.0)),
                ))
            elif line_type == "watch":
                watch_lines.append(WatchLineCharge(
                    phone_number=phone_number,
                    name=name,
                    associated_phone_number=associated_phone_number,
                    base_charge=float(line.get("base_charge", 0.0)),
                    equipment=float(line.get("equipment", 0.0)),
                    services=float(line.get("services", 0.0)),
                    one_time_charges=float(line.get("one_time_charges", 0.0)),
                ))

        # Remove account-wide Netflix charge from total charge
        total_charge = float(data.get("total_charge", 0.0))
        services_charge = float(data.get("services", 0.0))
        if services_charge > 0:
            total_charge -= services_charge

        return BillSummary(
            billing_period=data.get("billing_period", "Unknown"),
            paid_on=data.get("paid_on", "Unknown"),
            total_charge=total_charge,
            phone_lines=phone_lines,
            watch_lines=watch_lines,
            base_charge=phone_line_base_charge,
            equipment=float(data.get("equipment", 0.0)),
            services=0.0,  # Remove account-wide Netflix charge from total charge
            one_time_charges=float(data.get("one_time_charges", 0.0)),
        )

    def _calculate_splits(self, bill_summary: BillSummary) -> SplitsMap:
        """
        Calculate the amount each person owes.

        Iterates through phone lines, finds any associated watch lines,
        and creates a UserSplit combining both.

        Returns:
            SplitsMap: splitwise_user_id -> UserSplit
        """
        # Build watch line lookup: normalized associated_phone_number -> WatchLineCharge
        watch_by_assoc: dict[str, list[WatchLineCharge]] = {}
        for watch in bill_summary.watch_lines:
            assoc_norm = re.sub(r"\D", "", watch.associated_phone_number)
            watch_by_assoc.setdefault(assoc_norm, []).append(watch)

        splits: dict[int, UserSplit] = {}
        for phone_line in bill_summary.phone_lines:
            phone_norm = re.sub(r"\D", "", phone_line.phone_number)
            cfg = self.config_by_line.get(phone_norm, {})
            user_id = cfg.get("splitwise_user_id")
            if user_id is None:
                print(f"WARNING: No splitwise_user_id for {phone_line.phone_number}, skipping")
                continue

            amount = phone_line.total_charge
            lines = [phone_line.phone_number]

            # Build notes for this phone line
            note_parts = [
                f"{phone_line.phone_number}:",
                f"  Base: ${phone_line.base_charge:.2f}, Equipment: ${phone_line.equipment:.2f}, "
                f"Services: ${phone_line.services:.2f}, One-time: ${phone_line.one_time_charges:.2f}",
            ]

            # Add any watch lines associated with this phone line
            for watch in watch_by_assoc.get(phone_norm, []):
                amount += watch.total_charge
                lines.append(f"{watch.phone_number} (watch)")
                note_parts.append(
                    f"  Watch {watch.phone_number}: Base: ${watch.base_charge:.2f}, "
                    f"Equipment: ${watch.equipment:.2f}, Services: ${watch.services:.2f}, "
                    f"One-time: ${watch.one_time_charges:.2f}"
                )

            notes = "\n".join(note_parts)

            if user_id in splits:
                # Same user owns multiple phone lines — merge
                existing = splits[user_id]
                splits[user_id] = UserSplit(
                    splitwise_user_id=user_id,
                    name=existing.name,
                    amount=existing.amount + amount,
                    lines=existing.lines + lines,
                    notes=existing.notes + "\n" + notes,
                )
            else:
                splits[user_id] = UserSplit(
                    splitwise_user_id=user_id,
                    name=cfg.get("name", "Unknown"),
                    amount=amount,
                    lines=lines,
                    notes=notes,
                )

        return splits


def parse_bill(config: dict, pdf_path: Path, use_cache: bool, prompt_version: int | None = None) -> BillResult:
    """
    Convenience function to parse a T-Mobile bill using config dictionary.

    Args:
        config: Configuration dictionary with gemini section
        pdf_path: Path to the PDF file
        use_cache: If True, cache LLM responses locally
        prompt_version: Prompt version to use (None = latest)

    Returns:
        BillResult with bill summary and calculated splits
    """
    parser = BillParser(
        model=config["gemini"].get("model", "gemini-2.5-flash"),
        config=config,
        prompt_version=prompt_version,
    )
    return parser.parse_bill(pdf_path, use_cache=use_cache)


def print_summary(bill_result: BillResult) -> None:
    """Print a formatted summary of the parsed bill and calculated splits."""
    bill_summary = bill_result.summary

    print("\n" + "=" * 50)
    print("BILL SUMMARY")
    print("=" * 50)
    print(f"Billing Period: {bill_summary.billing_period}")
    print(f"Total Charge:   ${bill_summary.total_charge:.2f}")
    print(f"Base Amount:    ${bill_summary.base_charge:.2f}")
    print(f"Equipment:      ${bill_summary.equipment:.2f}")
    print(f"Services:       ${bill_summary.services:.2f}")

    print(f"\nPhone Lines ({len(bill_summary.phone_lines)}):")
    for line in bill_summary.phone_lines:
        print(f"  {line.phone_number} ({line.name}): ${line.total_charge:.2f}")
        print(f"    Base: ${line.base_charge:.2f}, Equipment: ${line.equipment:.2f}, Services: ${line.services:.2f}")

    if bill_summary.watch_lines:
        print(f"\nWatch Lines ({len(bill_summary.watch_lines)}):")
        for line in bill_summary.watch_lines:
            assoc = f" → {line.associated_phone_number}" if line.associated_phone_number else ""
            print(f"  {line.phone_number}{assoc} ({line.name}): ${line.total_charge:.2f}")
            print(f"    Base: ${line.base_charge:.2f}, Equipment: ${line.equipment:.2f}, Services: ${line.services:.2f}")

    # Print splits
    print("\n" + "=" * 50)
    print("SPLIT CALCULATION")
    print("=" * 50)
    for user_id, split in bill_result.splits.items():
        print(f"  {split.name} (ID: {user_id}): ${split.amount:.2f}")
        print(f"    Lines: {', '.join(split.lines)}")


if __name__ == "__main__":
    # Test the parser with a config file and sample bill
    import argparse
    import yaml

    init_env(GEMINI_ENV_VARS)

    parser = argparse.ArgumentParser(description="Parse a T-Mobile bill PDF and display the breakdown.")
    parser.add_argument("pdf_path", help="Path to the T-Mobile bill PDF file")
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to config file (default: config.yaml)")
    parser.add_argument("--cache", action="store_true", help="Cache LLM responses for faster re-runs")
    parser.add_argument("--prompt-version", type=int, default=None, help="Prompt version to use (default: latest)")
    args = parser.parse_args()

    with open(args.config, "r") as f:
        config = yaml.safe_load(f)
    result = parse_bill(config=config, pdf_path=Path(args.pdf_path), use_cache=args.cache, prompt_version=args.prompt_version)
    print_summary(result)
