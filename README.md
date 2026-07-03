# T-Mobile Bill to Splitwise Automation

Automates downloading T-Mobile bills, parsing them with Google Gemini, and posting per-line charges to Splitwise.

## Features

- **Automated Bill Download**: Uses Playwright to log into T-Mobile and download the latest PDF bill
- **AI-Powered Parsing**: Sends PDF to Google Gemini to extract line charges and shared fees
- **Smart Splitting**: Each person pays their line charge + equal share of taxes/fees
- **Splitwise Integration**: Automatically creates expenses with itemized breakdowns

## Prerequisites

- Python 3.10+
- Google Gemini API key
- Splitwise API credentials
- T-Mobile account credentials

## Installation

1. **Clone or copy the project:**
   ```bash
   cd tmobile_splitwise
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Install Playwright browsers:**
   ```bash
   playwright install chromium
   ```

5. **Copy and configure:**
   ```bash
   cp config.yaml config.yaml.backup  # Keep original as reference
   # Edit config.yaml with your credentials
   ```

## Configuration

Edit `config.yaml` with your credentials:

### T-Mobile Credentials
```yaml
tmobile:
  username: "your_email@example.com"
  password: "your_password"
  has_2fa: false  # Set true if you have 2FA enabled
```

### Gemini API Key
1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Create an API key
3. Add to config:
```yaml
gemini:
  api_key: "your_gemini_api_key"
  model: "gemini-1.5-flash"
```

### Splitwise API Setup

1. Go to [Splitwise Apps](https://secure.splitwise.com/apps)
2. Click "Register your application"
3. Fill in:
   - Application name: "TMobile Bill Splitter"
   - Description: "Splits T-Mobile bills"
   - Homepage URL: (can be blank or your website)
4. After registration, you'll get:
   - Consumer Key
   - Consumer Secret
   - API Key (click "API Keys" tab)
5. Add to config:
```yaml
splitwise:
  consumer_key: "your_consumer_key"
  consumer_secret: "your_consumer_secret"
  api_key: "your_api_key"
  group_name: 12345678
```

### Finding Group and User IDs

Run this command to list your Splitwise groups and member IDs:
```bash
python main.py --list-groups
```

Output will show:
```
Your Splitwise Groups:
--------------------------------------------------
  ID: 12345678, Name: Apartment
    - John Doe (ID: 11111111)
    - Jane Smith (ID: 22222222)
    - Bob Wilson (ID: 33333333)
--------------------------------------------------
```

### Phone Line Mapping

Map each phone number to a Splitwise user:
```yaml
line_mapping:
  paid_by: 11111111  # Who pays the bill (gets reimbursed)
  lines:
    - phone_number: "555-123-4567"
      splitwise_user_id: 11111111
      name: "John"
    - phone_number: "555-234-5678"
      splitwise_user_id: 22222222
      name: "Jane"
```

## Usage

### Full Workflow
```bash
# Run complete automation: download → parse → post to Splitwise
python main.py
```

### Dry Run (Test without posting)
```bash
# Parse bill but don't create Splitwise expense
python main.py --dry-run
```

### Use Existing PDF
```bash
# Skip download, use existing PDF
python main.py --skip-download --pdf-path ./bills/tmobile_bill_2024-01.pdf

# Or just use most recent PDF in bills directory
python main.py --skip-download
```

### List Splitwise Groups
```bash
python main.py --list-groups
```

## Monthly Scheduling

### Using Cron (Linux/macOS)

Add to crontab (`crontab -e`):
```bash
# Run on the 5th of every month at 9 AM
0 9 5 * * cd /path/to/tmobile_splitwise && /path/to/venv/bin/python main.py >> /var/log/tmobile_splitwise.log 2>&1
```

### Using Systemd Timer (Linux)

1. Create service file `/etc/systemd/system/tmobile-splitwise.service`:
```ini
[Unit]
Description=T-Mobile Bill to Splitwise Automation

[Service]
Type=oneshot
WorkingDirectory=/path/to/tmobile_splitwise
ExecStart=/path/to/venv/bin/python main.py
User=your_username
```

2. Create timer file `/etc/systemd/system/tmobile-splitwise.timer`:
```ini
[Unit]
Description=Run T-Mobile Splitwise automation monthly

[Timer]
OnCalendar=*-*-05 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

3. Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable tmobile-splitwise.timer
sudo systemctl start tmobile-splitwise.timer
```

### Using Task Scheduler (Windows)

1. Open Task Scheduler
2. Create Basic Task
3. Set trigger: Monthly, on day 5
4. Set action: Start a program
   - Program: `C:\path\to\venv\Scripts\python.exe`
   - Arguments: `main.py`
   - Start in: `C:\path\to\tmobile_splitwise`

## Troubleshooting

### T-Mobile Login Issues

- **2FA Required**: Set `has_2fa: true` in config. The browser will open visible and wait for you to approve the login on your phone.
- **Login Failed**: Check if T-Mobile updated their website. The script may need selector updates.
- **Screenshot**: Check `./bills/error_screenshot.png` for debugging.

### Gemini Parsing Issues

- **API Key Invalid**: Verify your Gemini API key at [Google AI Studio](https://aistudio.google.com/)
- **Parsing Errors**: Try switching to `gemini-1.5-pro` model for complex bills
- **Rate Limits**: Gemini has rate limits; wait a few minutes between retries

### Splitwise Issues

- **Authentication Failed**: Verify your API key and consumer credentials
- **Group Not Found**: Run `--list-groups` to verify the group ID
- **User Not in Group**: All users in line_mapping must be members of the group

## Project Structure

```
tmobile_splitwise/
├── config.yaml           # Your configuration (not in git)
├── main.py               # Entry point and orchestration
├── tmobile_scraper.py    # Playwright automation for T-Mobile
├── bill_parser.py        # Gemini PDF parsing logic
├── splitwise_client.py   # Splitwise API integration
├── requirements.txt      # Python dependencies
├── README.md             # This file
└── bills/                # Downloaded bills (created automatically)
```

## How Splitting Works

The script uses this formula for each person:

```
Amount Owed = Line Charge + (Total Shared Fees / Number of Lines)
```

Where:
- **Line Charge** = Individual line fee + device payment + line-specific add-ons
- **Total Shared Fees** = Plan cost + Taxes + Fees + Other charges - Credits

Example:
- Total bill: $200
- 4 phone lines
- Shared fees (taxes, plan): $80
- Per-person shared: $80 / 4 = $20
- John's line charge: $30 → John owes $50
- Jane's line charge: $25 → Jane owes $45

