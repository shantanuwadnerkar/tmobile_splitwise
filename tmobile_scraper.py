"""
T-Mobile Bill Scraper using Playwright

Automates login to my.t-mobile.com and downloads the latest bill PDF.
"""

import os
import sys
import time
import json
import shutil
import signal
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from utils import get_env, init_env, TMOBILE_ENV_VARS


class BrowserManager:
    """Manages a persistent browser process."""
    
    STATE_FILE = Path("/tmp/tmobile_scraper_browser.json")
    USER_DATA_DIR = Path("/tmp/tmobile_chrome_data")
    DEFAULT_CDP_PORT = 9222

    def cleanup(self):
        """
        Remove all tmp browser data to recover from crashes.
        Kills any stale browser process, removes user data dir and state file.
        """
        # 1. Kill stale process if recorded
        if self.STATE_FILE.exists():
            try:
                with open(self.STATE_FILE, "r") as f:
                    data = json.load(f)
                pid = data.get("pid")
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        print(f"Killed stale browser process (PID: {pid}).")
                    except OSError:
                        pass  # Process already gone
            except Exception:
                pass
            try:
                self.STATE_FILE.unlink()
                print(f"Removed state file: {self.STATE_FILE}")
            except Exception:
                pass

        # 2. Remove user data directory (contains lock files, cache, etc.)
        if self.USER_DATA_DIR.exists():
            try:
                shutil.rmtree(self.USER_DATA_DIR)
                print(f"Removed browser data directory: {self.USER_DATA_DIR}")
            except Exception as e:
                print(f"Warning: Could not fully remove {self.USER_DATA_DIR}: {e}")
    
    @staticmethod
    def get_browser_command() -> Optional[str]:
        """Find the Chrome/Chromium executable."""
        # Try system browsers first
        # Added google-chrome-stable and typical linux paths
        candidates = [
            "google-chrome", 
            "google-chrome-stable",
            "chromium-browser", 
            "chromium", 
            "/usr/bin/google-chrome"
        ]
        for cmd in candidates:
            if shutil.which(cmd):
                return cmd
                
        # Fallback to Playwright's bundled Chromium
        # We run this in a subprocess to avoid "Sync API inside asyncio loop" errors
        try:
            cmd = [
                sys.executable, "-c",
                "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                path = result.stdout.strip()
                if path and os.path.exists(path):
                    return path
        except Exception as e:
            print(f"Could not find Playwright Chromium via subprocess: {e}")
            
        return None

    def get_active_session(self) -> Optional[str]:
        """
        Check for an active browser session.
        Returns the CDP URL if the process is running, else None.
        """
        if not self.STATE_FILE.exists():
            return None
            
        try:
            with open(self.STATE_FILE, "r") as f:
                data = json.load(f)
            
            pid = data.get("pid")
            cdp_url = data.get("cdp_url")
            
            if not pid or not cdp_url:
                return None
                
            # Check if process is running
            try:
                os.kill(pid, 0) # Signal 0 checks if process exists
                return cdp_url
            except OSError:
                print(f"Browser process {pid} not found. Cleaning up stale data...")
                self.cleanup()
                return None
                
        except Exception as e:
            print(f"Error reading browser state: {e}")
            return None

    def launch_browser(self, headless: bool = False, _retry: bool = False) -> str:
        """
        Launch a new browser process and save its state.
        Returns the CDP URL.
        On failure, automatically cleans up stale data and retries once.
        """
        browser_cmd = self.get_browser_command()
        if not browser_cmd:
            raise Exception("No Chrome/Chromium executable found!")
            
        self.USER_DATA_DIR.mkdir(exist_ok=True)
        
        args = [
            browser_cmd,
            f"--remote-debugging-port={self.DEFAULT_CDP_PORT}",
            f"--user-data-dir={self.USER_DATA_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
            "--disable-gpu", 
            "--disable-dev-shm-usage"
        ]
        
        if headless:
            args.append("--headless")
            
        print(f"Launching browser: {' '.join(args)}")
        
        # Launch independent process
        process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True # Detach from parent
        )
        
        cdp_url = f"http://localhost:{self.DEFAULT_CDP_PORT}"
        
        # Save state
        with open(self.STATE_FILE, "w") as f:
            json.dump({
                "pid": process.pid,
                "cdp_url": cdp_url,
                "launch_time": datetime.now().isoformat()
            }, f)
            
        print(f"Browser launched (PID: {process.pid}). Waiting for CDP to be ready...")
        
        # Wait for CDP to be ready by polling the endpoint
        import urllib.request
        import urllib.error
        
        start_time = time.time()
        while time.time() - start_time < 10:  # Wait up to 10 seconds
            try:
                # Check if process is still alive
                if process.poll() is not None:
                    raise Exception(f"Browser process exited unexpectedly with code {process.returncode}")
                
                # Try to connect to the raw CDP endpoint
                with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=1) as response:
                    if response.status == 200:
                        print("CDP endpoint is ready.")
                        return cdp_url
            except (urllib.error.URLError, ConnectionRefusedError):
                time.sleep(1)
            except Exception as e:
                print(f"Error checking CDP status: {e}")
                # Auto-recovery: cleanup and retry once
                if not _retry:
                    print("Attempting auto-recovery: cleaning up stale browser data...")
                    try:
                        process.terminate()
                    except:
                        pass
                    self.cleanup()
                    return self.launch_browser(headless=headless, _retry=True)
                time.sleep(1)
                
        # If we get here, we timed out
        try:
            process.terminate()
        except:
            pass
        raise Exception("Timed out waiting for browser CDP to become ready.")


class TMobileScraper:
    """Scraper for downloading T-Mobile bills via browser automation."""

    TMOBILE_LOGIN_URL = "https://www.t-mobile.com/login"
    
    def __init__(self, username: str, has_2fa: bool = False, bills_dir: str = "./bills"):
        self.username = username
        self.password = get_env("TMOBILE_PASSWORD")
        self.has_2fa = has_2fa
        self.bills_dir = Path(bills_dir)
        self.bills_dir.mkdir(parents=True, exist_ok=True)
        self.browser_manager = BrowserManager()

    def download_latest_bill(self, headless: bool = True, keep_open: bool = False) -> str:
        with sync_playwright() as p:
            browser = None
            context = None
            is_cdp = False
            
            # 1. Attempt to find existing session
            cdp_url = self.browser_manager.get_active_session()
            
            if cdp_url:
                print(f"Found active browser session at {cdp_url}")
            else:
                print("No active session found. Launching new browser...")
                # Assuming if we launch it, we want it visible if 2FA is needed,
                # but respecting the headless arg passed in (default True, but config might switch it)
                # Note: The user said "headless=not has_2fa", so if 2FA is on, it's NOT headless.
                cdp_url = self.browser_manager.launch_browser(headless=headless)
            
            # 2. Connect
            try:
                print(f"Connecting to browser at {cdp_url}...")
                browser = p.chromium.connect_over_cdp(cdp_url)
                is_cdp = True
                
                if browser.contexts:
                    context = browser.contexts[0]
                else:
                    context = browser.new_context(viewport={"width": 1920, "height": 1080})
                    
            except Exception as e:
                raise Exception(f"Failed to connect to browser at {cdp_url}: {e}")

            try:
                if context.pages:
                    page = context.pages[0]
                else:
                    page = context.new_page()

                # --- Navigation Logic ---
                
                # Navigate to login page if we aren't already on t-mobile domain
                if "t-mobile.com" not in page.url:
                     print("Navigating to T-Mobile login...")
                     page.goto(self.TMOBILE_LOGIN_URL)
                else:
                    print(f"Already on {page.url}, continuing...")

                # Cookie Consent
                try:
                    reject_button = page.wait_for_selector(
                        'button:has-text("Reject"), button:has-text("Reject All"), button:has-text("Decline"), button[id*="reject"], button[class*="reject"]',
                        timeout=5000
                    )
                    if reject_button:
                        print("Rejecting cookies...")
                        reject_button.click()
                        page.wait_for_timeout(1000)
                except Exception:
                    pass

                # Check Login Status
                is_logged_in = False
                try:
                    if "dashboard" in page.url or page.locator('text="Bill & pay"').count() > 0:
                        is_logged_in = True
                        print("Already logged in.")
                except:
                    pass

                if not is_logged_in:
                    print("Opening Login menu...")
                    try:
                        my_account_btn = page.wait_for_selector(
                            'button:has-text("Login"), a:has-text("Login"), [aria-label*="Login"], [data-testid*="login"]',
                            timeout=10000
                        )
                        if my_account_btn:
                            my_account_btn.click()
                            page.wait_for_timeout(500)
                            
                            if page.locator('text="Logout"').is_visible():
                                print("Logout button found, assuming logged in.")
                            else:
                                print("Clicking Login...")
                                login_link = page.locator('a.unav-account__action--login')
                                login_link.wait_for(timeout=5000)
                                login_link.click()
                                page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception as e:
                        print(f"Could not find My Account menu: {e}")
                # Login Form
                if "signin" in page.url or "login" in page.url or page.locator('input#username, input[name="username"], input[id*="username"]').count() > 0:
                        print("Entering credentials...")
                        email_input = page.locator('input[placeholder="Email or phone number"], input[type="text"], input[type="email"]').first
                        email_input.wait_for(state="visible", timeout=20000)
                        email_input.click()
                        page.wait_for_timeout(300)
                        email_input.fill(self.username)
                        print(f"Entered username: {self.username}")

                        print("Clicking Next...")
                        next_button = page.locator('button:has-text("Next")').first
                        next_button.wait_for(state="visible", timeout=5000)
                        next_button.click()
                        
                        page.wait_for_load_state("domcontentloaded", timeout=30000)
                        page.wait_for_timeout(3000) 

                        # Handle auth method selection screen (Face ID vs Password)
                        try:
                            log_in_with_password = page.locator('text="Log in with password"').first
                            log_in_with_password.wait_for(state="visible", timeout=5000)
                            print("Auth method screen detected — clicking 'Log in with password'...")
                            log_in_with_password.click()
                            page.wait_for_load_state("domcontentloaded", timeout=15000)
                            page.wait_for_timeout(2000)
                        except Exception:
                            pass  # Screen not present, continue to password input

                        password_input = page.locator('input[placeholder="Password"], input[type="password"]').first
                        password_input.wait_for(state="visible", timeout=10000)
                        
                        # Attempt autofill
                        try:
                            password_input.fill(self.password)
                            print("Filled password automatically.")
                        except:
                            print("Could not fill password automatically.")

                        print("Waiting for password submission (manual or auto)...")
                        # We wait a bit to allow manual intervention if needed (especially if 2FA follows)
                        time.sleep(5)

                        # 2FA
                        if self.has_2fa:
                            print("\n" + "=" * 50)
                            print("2FA DETECTED - Waiting for manual approval...")
                            print("=" * 50 + "\n")
                            
                            try:
                                confirm_text = page.locator('text="Let\'s confirm it\'s you"')
                                confirm_text.wait_for(state="visible", timeout=10000)
                                continue_btn = page.locator('button:has-text("Continue"), a:has-text("Continue")')
                                continue_btn.wait_for(state="visible", timeout=5000)
                                continue_btn.click()
                            except:
                                pass

                        # Wait for dashboard
                        print("Waiting for dashboard...")
                        page.wait_for_load_state("domcontentloaded", timeout=300000)
                        
                # Historical Bills
                print("Navigating to historical bills page...")
                page.goto("https://www.t-mobile.com/bill/historical")
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                page.wait_for_timeout(5000)

                # Popup
                try:
                    dont_allow_btn = page.get_by_role("button", name="Don't allow")
                    dont_allow_btn.wait_for(state="visible", timeout=5000)
                    dont_allow_btn.click()
                    print("Dismissed notification popup")
                except:
                    pass

                print("Looking for latest bill...")
                
                # Robustly find the correct 'Summary PDF' element
                # We expect multiple elements, but the first one (index 0) is often a hidden or redirecting element.
                # We aim for the first *visible* element after index 0.
                summary_locator = page.get_by_text("Summary PDF")
                # Wait for at least one to be present
                try:
                    summary_locator.first.wait_for(state="attached", timeout=15000)
                except:
                    print("Warning: 'Summary PDF' text not found immediately.")

                count = summary_locator.count()
                print(f"Found {count} elements with text 'Summary PDF'")
                
                download_triggered = False
                download = None
                
                for i in range(count):
                    # Skip the first element as checks show it redirects to the dashboard
                    if i == 0:
                        continue

                    element = summary_locator.nth(i)
                    if not element.is_visible():
                        continue
                        
                    print(f"Found valid candidate at index {i}. Attempting download...")
                    
                    # Safety check: Ensure we haven't been redirected away
                    if "bill/historical" not in page.url:
                         print("Restoring historical bills page...")
                         page.goto("https://www.t-mobile.com/bill/historical", wait_until="load")
                         summary_locator = page.get_by_text("Summary PDF")
                         element = summary_locator.nth(i)

                    try:
                        with page.expect_download(timeout=30000) as download_info:
                            # Try clicking the element, fallback to parent if needed
                            try:
                                element.click(timeout=5000)
                            except:
                                print(" - direct click failed, clicking parent")
                                element.locator("xpath=..").click(timeout=5000, force=True)
                        
                        download = download_info.value
                        download_triggered = True
                        print("Download initiated successfully.")
                        break
                    except Exception as e:
                        print(f"Failed on candidate {i}: {e}. Trying next...")

                if not download_triggered:
                     raise Exception("Failed to trigger download. No valid 'Summary PDF' element found.")

                timestamp = datetime.now().strftime("%Y-%m")
                filename = f"tmobile_bill_{timestamp}.pdf"
                download_path = self.bills_dir / filename
                download.save_as(str(download_path))
                print(f"Bill downloaded successfully: {download_path}")

                return str(download_path)

            except Exception as e:
                # Screenshot
                try:
                    if context and context.pages:
                        context.pages[0].screenshot(path=str(self.bills_dir / "error_screenshot.png"))
                except:
                    pass
                raise Exception(f"Error executing scraper: {e}")

            finally:
                if keep_open:
                    print("\n" + "=" * 50)
                    print("Browser checking paused for debugging.")
                    print("Press Enter to disconnect (browser process will remain running)...")
                    print("=" * 50)
                    input()

                if browser:
                    print("Disconnecting from browser (process remains running)...")
                    browser.close()


def download_bill(config: dict, keep_open: bool = False) -> str:
    scraper = TMobileScraper(
        username=config["tmobile"]["username"],
        has_2fa=config["tmobile"].get("has_2fa", False),
        bills_dir=config["output"].get("bills_directory", "./bills")
    )
    # Headless decision logic can be inside scraper or here. 
    # If using BrowserManager, we might want to respect if an existing browser is visible or not.
    # But usually persistent browser implies visible (not headless) for debugging.
    # We'll pass the config preference.
    return scraper.download_latest_bill(headless=not config["tmobile"].get("has_2fa", False))


if __name__ == "__main__":
    import sys
    import yaml

    init_env(TMOBILE_ENV_VARS)

    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    try:
        pdf_path = download_bill(config)
        print(f"Successfully downloaded bill to: {pdf_path}")
    except Exception as e:
        print(f"Failed to download bill: {e}")
