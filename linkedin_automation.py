import csv
import os
import time
import random
import google.generativeai as genai
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    ElementClickInterceptedException,
    ElementNotInteractableException
)
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
import logging
from urllib.parse import urlparse, quote_plus
import re
import sys
import json
import pandas as pd
from datetime import datetime
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
import psutil
import tempfile
import platform
import shutil
import atexit
import uuid

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('linkedin_automation.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def _chromedriver_major_version() -> int:
        """Return driver’s major build (e.g., 74, 75, 118 …)."""
        from selenium.webdriver.chrome.service import Service
        try:
            srv = Service()                    # uses chromedriver on PATH
            # --version prints:  "ChromeDriver 74.0.3729.6 …"
            out = os.popen(f'"{srv.path}" --version').read()
            match = re.search(r'ChromeDriver (\d+)\.', out)
            return int(match.group(1)) if match else 0
        except Exception:
            return 0

def open_linkedin_tab(self):
    try:
        self.driver.execute_script("window.open('https://www.linkedin.com/feed','_blank');")
        self.driver.switch_to.window(self.driver.window_handles[-1])
        self.wait.until(lambda d: "https://www.linkedin.com" in d.current_url)
        time.sleep(3)
    except Exception as e:
        logger.warning(f"⚠️ Failed to open LinkedIn tab: {e}")

def _open_tab_and_wait(driver, url: str, title_contains: str = "", timeout: int = 15):
    """
    Opens `url` in a new tab, switches to it, and optionally waits
    until `title_contains` text appears in the document.title.
    """
    driver.execute_script(f"window.open('{url}', '_blank');")
    driver.switch_to.window(driver.window_handles[-1])
    if title_contains:
        WebDriverWait(driver, timeout).until(
            lambda d: title_contains.lower() in d.title.lower()
        )

class LinkedInAutomation:
    def __init__(self, email, password, api_key):
        self.email = email
        self.password = password
        self.api_key = api_key
        self.driver = None
        self.wait = None
        self.model = None
        self.tracked_profiles_file = 'messaged_profiles.json'
        self.tracked_profiles = set()
        self.persistent_profile_dir = None
        
        self.setup_driver()
        self.setup_ai()
        self.load_tracked_profiles()
        
        # Try to restore existing session
        self._load_session_cookies()

    def setup_driver(self):
        """Initialize Chrome with persistent session management"""
        try:
            if self.driver:
                try:
                    self.driver.quit()
                except Exception:
                    pass
            
            # Create persistent profile directory
            self.persistent_profile_dir = os.path.join(
                os.path.expanduser("~"), 
                ".linkedin_automation_profile"
            )
            os.makedirs(self.persistent_profile_dir, exist_ok=True)
            
            options = webdriver.ChromeOptions()
            options.add_argument(f"--user-data-dir={self.persistent_profile_dir}")
            options.add_argument("--profile-directory=LinkedIn")
            options.add_argument("--start-maximized")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-web-security")
            options.add_argument("--allow-running-insecure-content")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            
            # Disable notifications and popups
            prefs = {
                "profile.default_content_setting_values": {
                    "notifications": 2,
                },
                "profile.managed_default_content_settings": {
                    "images": 2
                }
            }
            options.add_experimental_option("prefs", prefs)
            
            self.driver = webdriver.Chrome(options=options)
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            self.wait = WebDriverWait(self.driver, 10)
            
            logger.info("✅ Chrome initialized with persistent profile")
            
        except Exception as e:
            logger.error(f"❌ Driver setup failed: {e}")
            raise


    def _cleanup_profile(self):
        """Clean up temporary profile directory"""
        if self.temp_profile_dir and os.path.exists(self.temp_profile_dir):
            try:
                shutil.rmtree(self.temp_profile_dir, ignore_errors=True)
            except Exception:
                pass
    
    def open_new_tab(self, url):
        """
        Opens a new tab in the same Chrome window and navigates to the provided URL.
        """
        # Open a new tab with the target URL
        self.driver.execute_script(f"window.open('{url}', '_blank');")
        # Switch to the newest tab
        self.driver.switch_to.window(self.driver.window_handles[-1])

    def login(self):
        """Enhanced login with session validation and persistence"""
        try:
            logger.info("🔐 Checking LinkedIn session...")
            
            # First, navigate to LinkedIn feed to check existing session
            self.driver.get("https://www.linkedin.com/feed")
            time.sleep(3)
            
            # Check if already logged in
            if self._is_logged_in():
                logger.info("✅ Already logged in! Session restored successfully")
                self._save_session_cookies()
                return True
            
            logger.info("🔄 No active session found, attempting login...")
            
            # Navigate to login page
            self.driver.get("https://www.linkedin.com/login")
            self.wait.until(EC.presence_of_element_located((By.ID, "username")))
            self.human_delay(1.5, 3)

            # Type email
            username_field = self.driver.find_element(By.ID, "username")
            logger.info("✏️ Typing email...")
            self.type_like_human(username_field, self.email)
            self.human_delay(1, 2)

            # Type password
            try:
                password_field = self.driver.find_element(By.ID, "password")
            except NoSuchElementException:
                logger.error("❌ Password field not found on login page.")
                return False

            if not self.password:
                logger.error("❌ LinkedIn password is empty or None. Cannot log in.")
                return False

            logger.info("✏️ Typing password...")
            self.type_like_human(password_field, self.password)
            
            # Click Login
            login_button = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            self.safe_click(login_button)

            # Wait for login success
            try:
                self.wait.until(lambda d: self._is_logged_in(), timeout=30)
                logger.info("✅ LinkedIn login successful!")
                
                # Save session for future use
                self._save_session_cookies()
                self._mark_session_active()
                
                self.human_delay(2, 4)
                return True

            except TimeoutException:
                if "checkpoint" in self.driver.current_url or "challenge" in self.driver.current_url:
                    logger.warning("⚠️ 2FA/Challenge page detected. Please complete manually.")
                    
                    # Wait for user to complete 2FA
                    logger.info("⏳ Waiting for manual 2FA completion...")
                    for i in range(120):  # Wait up to 2 minutes
                        time.sleep(1)
                        if self._is_logged_in():
                            logger.info("✅ 2FA completed successfully!")
                            self._save_session_cookies()
                            self._mark_session_active()
                            return True
                            
                    logger.error("❌ 2FA timeout - please try again")
                    return False
                    
                logger.error("❌ Login failed or timed out.")
                return False

        except Exception as e:
            logger.error(f"❌ Login exception: {e}")
            return False

    def _save_session_cookies(self):
        """Save current session cookies"""
        try:
            if "linkedin.com" in self.driver.current_url:
                cookies = self.driver.get_cookies()
                cookie_file = os.path.join(self.persistent_profile_dir, "linkedin_session.json")
                
                session_data = {
                    'cookies': cookies,
                    'user_email': self.email,
                    'timestamp': datetime.now().isoformat(),
                    'user_agent': self.driver.execute_script("return navigator.userAgent;")
                }
                
                with open(cookie_file, 'w') as f:
                    json.dump(session_data, f, indent=2)
                    
                logger.info("✅ Session cookies saved successfully")
                
        except Exception as e:
            logger.debug(f"Could not save session cookies: {e}")

    def _is_logged_in(self):
        """Enhanced login status check"""
        try:
            current_url = self.driver.current_url
            
            # Check URL patterns
            if any(pattern in current_url for pattern in [
                "linkedin.com/feed",
                "linkedin.com/in/",
                "linkedin.com/mynetwork",
                "linkedin.com/jobs",
                "linkedin.com/messaging"
            ]):
                return True
                
            # Check for navigation elements
            nav_selectors = [
                "[data-test-id='global-nav']",
                ".global-nav",
                ".global-nav__nav",
                "nav.global-nav"
            ]
            
            for selector in nav_selectors:
                if len(self.driver.find_elements(By.CSS_SELECTOR, selector)) > 0:
                    return True
                    
            # Check for profile icon
            profile_selectors = [
                ".global-nav__primary-item--profile",
                ".global-nav__me-photo",
                "[data-test-id='nav-profile-photo']"
            ]
            
            for selector in profile_selectors:
                if len(self.driver.find_elements(By.CSS_SELECTOR, selector)) > 0:
                    return True
                    
            return False
            
        except Exception:
            return False

    def _fet_chrome_user_data_dir(self):
        """Automatically detect Chrome user data directory based on OS"""
        system = platform.system()
        
        if system == "Windows":
            base_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
            if os.path.exists(base_path):
                return base_path
            # Alternative Windows path
            alt_path = os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Google", "Chrome", "User Data")
            if os.path.exists(alt_path):
                return alt_path
                
        elif system == "Darwin":  # macOS
            base_path = os.path.expanduser("~/Library/Application Support/Google/Chrome")
            if os.path.exists(base_path):
                return base_path
                
        else:  # Linux
            base_path = os.path.expanduser("~/.config/google-chrome")
            if os.path.exists(base_path):
                return base_path
            # Alternative Linux path
            alt_path = os.path.expanduser("~/.config/chromium")
            if os.path.exists(alt_path):
                return alt_path
        
        return None

    def _setup_with_copied_profile(self, options):
        """Setup using a copied Chrome profile to avoid 'already in use' errors"""
        try:
            chrome_user_data = self._get_chrome_user_data_dir()
            if not chrome_user_data:
                logger.info("Chrome user data directory not found, skipping profile method")
                return False
                
            # Find the Default profile or first available profile
            profile_dir = None
            for possible_profile in ["Default", "Profile 1", "Profile 2"]:
                potential_path = os.path.join(chrome_user_data, possible_profile)
                if os.path.exists(potential_path):
                    profile_dir = potential_path
                    break
                    
            if not profile_dir:
                logger.info("No Chrome profiles found")
                return False
                
            # Create a temporary directory for our automation profile
            automation_profile_base = os.path.join(tempfile.gettempdir(), "linkedin_automation_chrome")
            if os.path.exists(automation_profile_base):
                shutil.rmtree(automation_profile_base, ignore_errors=True)
            os.makedirs(automation_profile_base, exist_ok=True)
            
            # Copy essential profile files for session persistence
            automation_profile_dir = os.path.join(automation_profile_base, "Default")
            os.makedirs(automation_profile_dir, exist_ok=True)
            
            # Copy key files for login persistence
            files_to_copy = [
                "Cookies", "Local State", "Login Data", "Preferences", 
                "Network Action Predictor", "Local Storage"
            ]
            
            for file_name in files_to_copy:
                src_file = os.path.join(profile_dir, file_name)
                dst_file = os.path.join(automation_profile_dir, file_name)
                
                try:
                    if os.path.isfile(src_file):
                        shutil.copy2(src_file, dst_file)
                    elif os.path.isdir(src_file):
                        if os.path.exists(dst_file):
                            shutil.rmtree(dst_file)
                        shutil.copytree(src_file, dst_file)
                except (PermissionError, OSError) as e:
                    logger.debug(f"Could not copy {file_name}: {e}")
                    continue
                    
            # Configure Chrome to use our automation profile
            options.add_argument(f"--user-data-dir={automation_profile_base}")
            options.add_argument("--profile-directory=Default")
            
            # Store the profile path for cleanup
            self.automation_profile_path = automation_profile_base
            
            self.driver = webdriver.Chrome(options=options)
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            self.wait = WebDriverWait(self.driver, 10)
            self.driver.set_page_load_timeout(30)
            self.driver.implicitly_wait(5)
            
            logger.info("✅ Driver initialized with copied Chrome profile for session persistence")
            return True
            
        except Exception as e:
            logger.debug(f"Copied profile setup failed: {e}")
            return False

    def _setup_with_cookies(self, options):
        """Fallback setup with cookie-based session persistence"""
        # Create a dedicated directory for our automation
        automation_dir = os.path.join(tempfile.gettempdir(), "linkedin_automation_simple")
        os.makedirs(automation_dir, exist_ok=True)
        
        options.add_argument(f"--user-data-dir={automation_dir}")
        
        self.driver = webdriver.Chrome(options=options)
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.wait = WebDriverWait(self.driver, 10)
        self.driver.set_page_load_timeout(30)
        self.driver.implicitly_wait(5)
        
        # Load saved cookies if they exist
        self._load_linkedin_cookies()
        
        logger.info("✅ Driver initialized with cookie-based session persistence")

    def _save_linkedin_cookies(self):
        """Save LinkedIn cookies after successful login"""
        try:
            if "linkedin.com" in self.driver.current_url:
                cookies = self.driver.get_cookies()
                cookie_file = os.path.join(tempfile.gettempdir(), "linkedin_cookies.json")
                
                with open(cookie_file, 'w') as f:
                    json.dump(cookies, f)
                logger.info("✅ LinkedIn cookies saved for future sessions")
        except Exception as e:
            logger.debug(f"Could not save cookies: {e}")

    def _load_session_cookies(self):
        """Load saved session cookies"""
        try:
            cookie_file = os.path.join(self.persistent_profile_dir, "linkedin_session.json")
            
            if os.path.exists(cookie_file):
                with open(cookie_file, 'r') as f:
                    session_data = json.load(f)
                    
                # Check if session is for current user
                if session_data.get('user_email') != self.email:
                    logger.info("🔄 Session is for different user, skipping cookie load")
                    return False
                    
                # Navigate to LinkedIn first
                self.driver.get("https://www.linkedin.com")
                time.sleep(1)
                
                # Load cookies
                for cookie in session_data.get('cookies', []):
                    try:
                        self.driver.add_cookie(cookie)
                    except Exception as e:
                        logger.debug(f"Could not add cookie: {e}")
                        
                logger.info("✅ Previous session cookies loaded")
                return True
                
        except Exception as e:
            logger.debug(f"Could not load session cookies: {e}")
            
        return False
    def _mark_session_active(self):
        """Mark session as active"""
        try:
            session_file = os.path.join(self.persistent_profile_dir, "session_status.json")
            session_status = {
                'active': True,
                'user_email': self.email,
                'last_activity': datetime.now().isoformat()
            }
            
            with open(session_file, 'w') as f:
                json.dump(session_status, f, indent=2)
                
        except Exception as e:
            logger.debug(f"Could not mark session active: {e}")

    def _check_session_health(self):
        """Check if current session is healthy"""
        try:
            if not self.driver:
                return False
                
            # Try to access a LinkedIn page
            current_url = self.driver.current_url
            if "linkedin.com" not in current_url:
                self.driver.get("https://www.linkedin.com/feed")
                time.sleep(2)
                
            return self._is_logged_in()
            
        except Exception:
            return False

    def ensure_linkedin_session(self):
        """Ensure we have an active LinkedIn session"""
        if not self._check_session_health():
            logger.info("🔄 Session lost, re-establishing connection...")
            return self.login()
        return True
    
    def close(self):
        """Enhanced cleanup"""
        if self.driver:
            try:
                # Save cookies before closing
                self._save_linkedin_cookies()
            except:
                pass
            self.driver.quit()
            
        # Clean up automation profile if it exists
        if hasattr(self, 'automation_profile_path') and os.path.exists(self.automation_profile_path):
            try:
                shutil.rmtree(self.automation_profile_path, ignore_errors=True)
            except:
                pass

    def setup_ai(self):
        """Initialize Gemini AI"""
        try:
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel('gemini-1.5-flash')
            logger.info("✅ Gemini AI initialized successfully")
        except Exception as e:
            logger.error(f"❌ Gemini AI initialization failed: {e}")
            self.model = None
            
    def load_tracked_profiles(self):
        """Load previously messaged profiles to avoid duplicates"""
        if os.path.exists(self.tracked_profiles_file):
            try:
                with open(self.tracked_profiles_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.tracked_profiles = set(data)
                    logger.info(f"✅ Loaded {len(self.tracked_profiles)} previously messaged profiles")
            except Exception as e:
                logger.warning(f"⚠️ Could not load tracked profiles: {e}")
                self.tracked_profiles = set()
        else:
            self.tracked_profiles = set()
            
    def save_tracked_profiles(self):
        """Save tracked profiles to file"""
        try:
            with open(self.tracked_profiles_file, 'w', encoding='utf-8') as f:
                json.dump(list(self.tracked_profiles), f, ensure_ascii=False, indent=2)
                logger.info(f"✅ Saved {len(self.tracked_profiles)} tracked profiles")
        except Exception as e:
            logger.error(f"❌ Could not save tracked profiles: {e}")
            
    def is_profile_messaged(self, profile_url):
        """Check if profile has been messaged before"""
        return profile_url in self.tracked_profiles
        
    def add_profile_to_tracked(self, profile_url):
        """Add profile to tracked list"""
        self.tracked_profiles.add(profile_url)
        self.save_tracked_profiles()
        logger.info(f"📝 Added profile to tracked list: {profile_url}")
        
    def human_delay(self, min_seconds=1, max_seconds=3):
        """Add human-like delays"""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)
        
    def type_like_human(self, element, text):
        """Type text with human-like delays"""
        element.clear()
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.05, 0.2))

    def _handle_connection_modal(self, name):
        """Handle the connection modal popup"""
        try:
            # Try to add note
            try:
                add_note_btn = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Add a note')]"))
                )
                add_note_btn.click()
                time.sleep(1)
                
                note_area = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "textarea[name='message']"))
                )
                
                note_text = f"Hi {name.split()[0]}, I'd love to connect and learn about your professional journey!"
                note_area.send_keys(note_text)
                time.sleep(1)
                
            except TimeoutException:
                logger.info(f"No note option for {name}")
            
            # Click send
            send_selectors = [
                "//button[normalize-space()='Send now']",
                "//button[normalize-space()='Send']",
                "//button[contains(@aria-label,'Send')]"
            ]
            
            for selector in send_selectors:
                try:
                    send_btn = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    send_btn.click()
                    
                    # Wait for confirmation
                    WebDriverWait(self.driver, 5).until(
                        EC.presence_of_element_located((
                            By.XPATH, 
                            "//button[normalize-space()='Pending'] | //div[contains(text(), 'Invitation sent')]"
                        ))
                    )
                    return True
                    
                except TimeoutException:
                    continue
            
            return False
            
        except Exception as e:
            logger.error(f"Modal handling error for {name}: {e}")
            return False

    def _attempt_connection(self, button, name):
        """Attempt to connect with a person"""
        try:
            # Scroll and click
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
            time.sleep(1)
            button.click()
            time.sleep(2)
            
            # Handle modal
            return self._handle_connection_modal(name)
            
        except Exception as e:
            logger.warning(f"Connection attempt failed for {name}: {e}")
            return False

    def safe_connect_with_recovery(self, button, name):
        """Connect with session recovery on failure"""
        max_attempts = 2
        
        for attempt in range(max_attempts):
            try:
                # Scroll to button
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", button
                )
                time.sleep(1)
                
                # Click connect
                button.click()
                time.sleep(2)
                
                # Handle modal
                return self.handle_connect_modal_safe(name)
                
            except Exception as e:
                logger.warning(f"Connect attempt {attempt + 1} failed for {name}: {e}")
                
                if attempt < max_attempts - 1:
                    # Try to recover session
                    try:
                        self.driver.current_url
                    except Exception:
                        logger.info("Recovering session...")
                        self.setup_driver()
                        if not self.login():
                            return False
                    time.sleep(2)
                
        return False

            
    def safe_click(self, element):
        """Safely click an element with fallback methods"""
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", element)
            time.sleep(random.uniform(0.5, 1.5))
            element.click()
            return True
        except (ElementClickInterceptedException, ElementNotInteractableException):
            try:
                ActionChains(self.driver).move_to_element(element).pause(0.5).click().perform()
                return True
            except Exception as e:
                logger.warning(f"Safe click failed: {e}")
                return False
                
    def extract_profile_data(self):
        """Extract profile data from current LinkedIn profile page"""
        profile_data = {}
        
        try:
            # Wait for profile to load
            self.wait.until(
                EC.any_of(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "h1")),
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".pv-text-details__left-panel"))
                )
            )
            
            # Extract name
            name_selectors = [
                "h1.text-heading-xlarge",
                ".pv-text-details__left-panel h1",
                "[data-test-id='profile-name'] h1",
                ".ph5 h1"
            ]
            
            for selector in name_selectors:
                try:
                    name_elem = self.driver.find_element(By.CSS_SELECTOR, selector)
                    profile_data['extracted_name'] = name_elem.text.strip()
                    logger.info(f"📝 Extracted name: {profile_data['extracted_name']}")
                    break
                except NoSuchElementException:
                    continue
                    
            # Extract headline
            headline_selectors = [
                ".text-body-medium.break-words",
                ".pv-text-details__left-panel .text-body-medium",
                "[data-test-id='profile-headline']"
            ]
            
            for selector in headline_selectors:
                try:
                    headline_elem = self.driver.find_element(By.CSS_SELECTOR, selector)
                    headline_text = headline_elem.text.strip()
                    if headline_text and headline_text != profile_data.get('extracted_name', ''):
                        profile_data['extracted_headline'] = headline_text
                        logger.info(f"💼 Extracted headline: {headline_text[:50]}...")
                        break
                except NoSuchElementException:
                    continue
                    
            # Extract about section
            about_selectors = [
                "[data-test-id='about-section'] .pv-shared-text-with-see-more span[aria-hidden='true']",
                ".pv-about-section .pv-shared-text-with-see-more span"
            ]
            
            for selector in about_selectors:
                try:
                    about_elem = self.driver.find_element(By.CSS_SELECTOR, selector)
                    about_text = about_elem.text.strip()
                    if about_text:
                        profile_data['about_snippet'] = about_text[:150] + "..." if len(about_text) > 150 else about_text
                        logger.info(f"📄 Extracted about: {profile_data['about_snippet'][:50]}...")
                        break
                except NoSuchElementException:
                    continue
                    
            # Set defaults
            if not profile_data.get('extracted_name'):
                profile_data['extracted_name'] = "Professional"
            if not profile_data.get('about_snippet'):
                profile_data['about_snippet'] = ""
                
        except Exception as e:
            logger.warning(f"⚠️ Profile data extraction failed: {e}")
            profile_data = {
                'extracted_name': 'Professional',
                'extracted_headline': '',
                'about_snippet': ''
            }
            
        return profile_data
        
    def generate_message(self, name, company, role, service_1="", service_2="", profile_data=None):
        """Generate personalized LinkedIn message using AI"""
        if not self.model:
            fallback_msg = f"Hi {name}, I'm impressed by your work as {role} at {company}. I'd love to connect and learn more about your experience in {service_1 or 'your field'}. Looking forward to connecting!"
            return fallback_msg[:280]
            
        actual_name = profile_data.get('extracted_name', name) if profile_data else name
        about_snippet = profile_data.get('about_snippet', '') if profile_data else ''
        headline = profile_data.get('extracted_headline', role) if profile_data else role
        # Enhanced prompt for better personalization
        message_template = f"""You are a professional networking assistant. Write a personalized, concise, and professional LinkedIn connection request note (under 300 characters).

        **Context about me (the sender):**
        {service_1}

        **Information about the person I'm connecting with:**
        - Name: {actual_name}
        - Company: {company}
        - Headline: {headline}
        - Their 'About' section snippet: "{about_snippet}"

        **Instructions:**
        1.  Start with "Hi {actual_name.split()[0]},".
        2.  Briefly mention a specific, impressive detail from their headline or company.
        3.  State the reason for connecting clearly and concisely.
        4.  Keep it professional, friendly, and under the character limit.
        5.  **Crucially, return ONLY the message text.** Do not include any extra labels, quotes, or explanations.

        **Example:** "Hi Jane, I was impressed by your work in product strategy at TechCorp. I'm also in the product space and would love to connect and exchange ideas. Thanks!"

Return ONLY the message text, no labels or formatting."""

        for attempt in range(3):
            try:
                response = self.model.generate_content(message_template)
                message = response.text.strip()
                
                # Clean up message
                message = re.sub(r'^(Message:|Icebreaker:)\s*', '', message, flags=re.IGNORECASE)
                message = message.strip('"\'[]')
                
                if len(message) > 280:
                    message = message[:277] + "..."
                    
                return message
                
            except Exception as e:
                if "429" in str(e) or "ResourceExhausted" in str(e):
                    wait_time = 30 * (attempt + 1)
                    logger.warning(f"⏳ AI rate limit hit. Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"❌ AI generation error: {e}")
                    break
                    
        # Fallback message
        fallback_msg = f"Hi {actual_name}, I'm impressed by your {role} work at {company}. I'd love to connect and exchange insights about {service_1 or 'industry trends'}. Looking forward to connecting!"
        return fallback_msg[:280]
    
    def send_message(self, message, name, company):
        """Enhanced send_message function with standardized priority order and user confirmation"""
        logger.info(f"🚀 Starting outreach process for {name} at {company}")
        
        try:
            # Wait for page to load completely
            WebDriverWait(self.driver, 15).until(
                lambda d: d.execute_script('return document.readyState') == 'complete'
            )
            self.human_delay(2, 4)
        except TimeoutException:
            logger.warning("⚠️ Page load timeout - proceeding anyway")

        # Extract profile data for better personalization
        profile_data = self.extract_profile_data()

        # PRIORITY 1: Try connection request with note (HIGHEST SUCCESS RATE)
        logger.info("🎯 Priority 1: Attempting connection request with personalized note...")
        if self.send_connection_request_with_note_enhanced(message, name):
            logger.info(f"✅ Successfully sent connection request with note to {name}")
            return True

        # PRIORITY 2: Try connection request without note (FALLBACK)
        logger.info("🎯 Priority 2: Attempting connection request without note...")
        if self.send_connection_request_without_note_enhanced(name):
            logger.info(f"✅ Successfully sent connection request without note to {name}")
            return True

        # PRIORITY 3: Try direct message (LAST RESORT - only for existing connections)
        logger.info("🎯 Priority 3: Attempting direct message...")
        if self.send_direct_message_enhanced(message, name):
            logger.info(f"✅ Successfully sent direct message to {name}")
            return True

        # If all methods fail
        logger.error(f"❌ All outreach methods failed for {name}")
        return False

    def send_connection_request_without_note_enhanced(self, name):
        """Enhanced connection request without note"""
        logger.info(f"🤝 Attempting to send connection request without note to {name}...")

        # Find Connect button with multiple selectors
        connect_button_selectors = [
            ("css", "button.artdeco-button.artdeco-button--2.artdeco-button--primary[aria-label*='Connect']"),
            ("xpath", "//button[contains(@aria-label, 'Connect') and contains(@class, 'artdeco-button--primary')]"),
            ("xpath", "//button[.//span[text()='Connect']]"),
            ("css", "button[aria-label*='Connect'][class*='artdeco-button']")
        ]

        connect_button = self.find_element_safe(connect_button_selectors, timeout=8)
        
        if not connect_button:
            logger.info("🔍 Connect button not found, checking More menu...")
            more_button_selectors = [
                ("css", "button[aria-label*='More actions']"),
                ("xpath", "//button[contains(@aria-label, 'More actions')]"),
                ("xpath", "//button[.//span[text()='More']]"),
                ("css", "button.artdeco-dropdown__trigger")
            ]

            more_button = self.find_element_safe(more_button_selectors, timeout=5)
            if more_button and self.safe_click(more_button):
                logger.info("✅ More menu clicked")
                self.human_delay(1, 2)
                
                dropdown_connect_selectors = [
                    ("xpath", "//div[contains(@class, 'artdeco-dropdown__content')]//span[text()='Connect']/ancestor::*[1]"),
                    ("css", "[aria-expanded='true'] [aria-label*='Connect']"),
                    ("xpath", "//div[contains(@class, 'artdeco-dropdown')]//span[text()='Connect']/parent::*")
                ]

                connect_button = self.find_element_safe(dropdown_connect_selectors, timeout=5)
                if not connect_button:
                    logger.error("❌ Connect option not found in More menu")
                    return False
            else:
                logger.error("❌ Connect button not found")
                return False

        # Click Connect button
        if not self.safe_click(connect_button):
            logger.error("❌ Failed to click Connect button")
            return False

        logger.info("✅ Connect button clicked")
        self.human_delay(2, 3)

        try:
            # Look for Send button (skip adding note)
            send_request_selectors = [
                ("css", "button[aria-label='Send now']"),
                ("xpath", "//button[@aria-label='Send now']"),
                ("css", "button[aria-label*='Send invitation']"),
                ("xpath", "//button[contains(@aria-label, 'Send') and contains(@class, 'artdeco-button--primary')]"),
                ("xpath", "//button[.//span[text()='Send']]"),
                ("css", "button.artdeco-button--primary[aria-label*='Send']")
            ]

            send_button = self.find_element_safe(send_request_selectors, timeout=10)
            if send_button and self.safe_click(send_button):
                logger.info(f"✅ Connection request without note sent successfully to {name}!")
                self.human_delay(2, 4)
                return True
            else:
                logger.error("❌ Could not find or click send button")
                return False

        except Exception as e:
            logger.error(f"❌ Error sending connection request without note: {e}")
            self.driver.save_screenshot(f"connection_no_note_error_{name}_{int(time.time())}.png")
            return False
        
    def send_connection_request_without_note(driver, name):
        """Send connection request without a personalized note"""
        logger.info(f"🤝 Attempting to send connection request without note to {name}...")
        
        # Find Connect button
        connect_button_selectors = [
            ("css", "button.artdeco-button.artdeco-button--2.artdeco-button--primary[aria-label*='Connect']"),
            ("xpath", "//button[contains(@aria-label, 'Connect') and contains(@class, 'artdeco-button--primary')]"),
            ("xpath", "//button[.//span[text()='Connect']]"),
            ("css", "button[aria-label*='Connect'][class*='artdeco-button']")
        ]
        
        connect_button = find_element_safe(driver, connect_button_selectors, timeout=8)
        if not connect_button:
            logger.info("🔍 Connect button not found, checking More menu...")
            more_button_selectors = [
                ("css", "button[aria-label*='More actions']"),
                ("xpath", "//button[contains(@aria-label, 'More actions')]"),
                ("xpath", "//button[.//span[text()='More']]"),
                ("css", "button.artdeco-dropdown__trigger")
            ]
            more_button = find_element_safe(driver, more_button_selectors, timeout=5)
            if more_button and safe_click(driver, more_button):
                logger.info("✅ More menu clicked")
                human_delay(1, 2)
                dropdown_connect_selectors = [
                    ("xpath", "//div[contains(@class, 'artdeco-dropdown__content')]//span[text()='Connect']/ancestor::*[1]"),
                    ("css", "[aria-expanded='true'] [aria-label*='Connect']"),
                    ("xpath", "//div[contains(@class, 'artdeco-dropdown')]//span[text()='Connect']/parent::*")
                ]
                connect_button = find_element_safe(driver, dropdown_connect_selectors, timeout=5)
                if not connect_button:
                    logger.error("❌ Connect option not found in More menu")
                    return False
            else:
                logger.error("❌ Connect button not found")
                return False
        
        # Click Connect button
        if not safe_click(driver, connect_button):
            logger.error("❌ Failed to click Connect button")
            return False
        
        logger.info("✅ Connect button clicked")
        human_delay(2, 3)
        
        try:
            # Look for Send button (skip adding note)
            send_request_selectors = [
                ("css", "button[aria-label='Send now']"),
                ("xpath", "//button[@aria-label='Send now']"),
                ("css", "button[aria-label*='Send invitation']"),
                ("xpath", "//button[contains(@aria-label, 'Send') and contains(@class, 'artdeco-button--primary')]"),
                ("xpath", "//button[.//span[text()='Send']]"),
                ("css", "button.artdeco-button--primary[aria-label*='Send']")
            ]
            
            send_button = find_element_safe(driver, send_request_selectors, timeout=10)
            if send_button and safe_click(driver, send_button):
                logger.info(f"✅ Connection request without note sent successfully to {name}!")
                human_delay(2, 4)
                return True
            else:
                logger.error("❌ Could not find or click send button")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error sending connection request without note: {e}")
            driver.save_screenshot(f"connection_no_note_error_{name}_{int(time.time())}.png")
            return False

    def send_connection_request_with_note_enhanced(self, message, name):
        """Enhanced connection request with note - based on LinkedIn_automation_script.py"""
        logger.info(f"🤝 Attempting to send connection request with note to {name}...")
        
        # Find Connect button with multiple selectors
        connect_button_selectors = [
            ("css", "button.artdeco-button.artdeco-button--2.artdeco-button--primary[aria-label*='Connect']"),
            ("xpath", "//button[contains(@aria-label, 'Connect') and contains(@class, 'artdeco-button--primary')]"),
            ("xpath", "//button[.//span[text()='Connect']]"),
            ("css", "button[aria-label*='Connect'][class*='artdeco-button']")
        ]
        
        connect_button = self.find_element_safe(connect_button_selectors, timeout=8)
        
        if not connect_button:
            logger.info("🔍 Connect button not found, checking More menu...")
            more_button_selectors = [
                ("css", "button[aria-label*='More actions']"),
                ("xpath", "//button[contains(@aria-label, 'More actions')]"),
                ("xpath", "//button[.//span[text()='More']]"),
                ("css", "button.artdeco-dropdown__trigger")
            ]
            
            more_button = self.find_element_safe(more_button_selectors, timeout=5)
            if more_button and self.safe_click(more_button):
                logger.info("✅ More menu clicked")
                self.human_delay(1, 2)
                
                dropdown_connect_selectors = [
                    ("xpath", "//div[contains(@class, 'artdeco-dropdown__content')]//span[text()='Connect']/ancestor::*[1]"),
                    ("css", "[aria-expanded='true'] [aria-label*='Connect']"),
                    ("xpath", "//div[contains(@class, 'artdeco-dropdown')]//span[text()='Connect']/parent::*")
                ]
                
                connect_button = self.find_element_safe(dropdown_connect_selectors, timeout=5)
                
                if not connect_button:
                    logger.error("❌ Connect option not found in More menu")
                    return False
            else:
                logger.error("❌ Connect button not found")
                return False

        # Click Connect button
        if not self.safe_click(connect_button):
            logger.error("❌ Failed to click Connect button")
            return False
        
        logger.info("✅ Connect button clicked")
        self.human_delay(2, 3)

        try:
            # Look for "Add a note" button
            add_note_selectors = [
                ("css", "button[aria-label='Add a note']"),
                ("xpath", "//button[@aria-label='Add a note']"),
                ("xpath", "//button[.//span[text()='Add a note']]"),
                ("css", "button[aria-label*='Add a note']"),
                ("xpath", "//button[contains(text(), 'Add a note')]")
            ]
            
            add_note_button = self.find_element_safe(add_note_selectors, timeout=8)
            
            if not add_note_button:
                logger.info("❌ Add a note button not found - cannot send with note")
                # Close the connection dialog if it's open
                try:
                    close_button = self.driver.find_element(By.CSS_SELECTOR, "button[aria-label*='Dismiss'], button[aria-label*='Cancel']")
                    self.safe_click(close_button)
                except:
                    pass
                return False

            # Click "Add a note"
            if not self.safe_click(add_note_button):
                logger.error("❌ Failed to click Add a note button")
                return False
            
            logger.info("✅ Add a note clicked")
            self.human_delay(1, 2)

            # Find and fill note text area
            note_area_selectors = [
                ("css", "textarea[name='message']"),
                ("css", "#custom-message"),
                ("css", "textarea[aria-label*='note']"),
                ("css", ".connect-note-form textarea"),
                ("xpath", "//textarea[@name='message']")
            ]
            
            note_area = self.find_element_safe(note_area_selectors, timeout=8)
            if not note_area:
                logger.error("❌ Could not find note text area")
                return False

            # Type the personalized message
            self.type_like_human(note_area, message)
            logger.info("✅ Personalized note added successfully")
            self.human_delay(1, 2)

            # Find and click Send button
            send_request_selectors = [
                ("css", "button[aria-label='Send now']"),
                ("xpath", "//button[@aria-label='Send now']"),
                ("css", "button[aria-label*='Send invitation']"),
                ("xpath", "//button[contains(@aria-label, 'Send')]"),
                ("xpath", "//button[.//span[text()='Send']]")
            ]
            
            send_button = self.find_element_safe(send_request_selectors, timeout=10)
            if send_button and self.safe_click(send_button):
                logger.info(f"✅ Connection request with note sent successfully to {name}!")
                self.human_delay(2, 4)
                return True
            else:
                logger.error("❌ Could not find or click send button")
                return False

        except Exception as e:
            logger.error(f"❌ Error sending connection request with note: {e}")
            self.driver.save_screenshot(f"connection_note_error_{name}_{int(time.time())}.png")
            return False
        
    def send_direct_message_enhanced(self, message, name):
        """Enhanced direct message function with robust button detection"""
        logger.info(f"🔍 Attempting to locate Message button for {name}...")

        # Multiple selector strategies for the Message button
        message_button_selectors = [
            ("css", "button[aria-label*='Message'][class*='artdeco-button']"),
            ("css", "button.artdeco-button--primary[aria-label*='Message']"),
            ("xpath", "//button[contains(@aria-label, 'Message') and contains(@class, 'artdeco-button')]"),
            ("xpath", "//button[.//span[text()='Message']]"),
            ("css", "button[data-control-name*='message']"),
            ("css", "button.pvs-profile-actions__action[aria-label*='Message']"),
            ("css", "button[aria-label*='Message']"),
            ("xpath", "//button[contains(text(), 'Message')]"),
            ("xpath", "//span[text()='Message']/parent::button")
        ]

        msg_btn = None
        for selector_type, selector in message_button_selectors:
            try:
                if selector_type == "xpath":
                    msg_btn = WebDriverWait(self.driver, 6).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    msg_btn = WebDriverWait(self.driver, 6).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )

                if msg_btn and msg_btn.is_displayed() and msg_btn.is_enabled():
                    logger.info(f"✅ Message button found using: {selector}")
                    break
                else:
                    msg_btn = None
            except (TimeoutException, NoSuchElementException):
                continue

        if not msg_btn:
            logger.info("❌ No Message button found - user may not be a 1st degree connection")
            return False

        # Click message button
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", msg_btn)
            self.human_delay(1, 2)

            if not self.safe_click(msg_btn):
                ActionChains(self.driver).move_to_element(msg_btn).click().perform()

            logger.info("✅ Message button clicked successfully")
            self.human_delay(2, 3)
        except Exception as e:
            logger.error(f"❌ Failed to click Message button: {e}")
            return False

        # Enhanced message composition
        compose_selectors = [
            ("css", ".msg-form__contenteditable"),
            ("css", "[data-test-id='message-composer-input']"),
            ("css", "div[role='textbox'][contenteditable='true']"),
            ("xpath", "//textarea[@aria-label='Write a message…']"),
            ("css", ".msg-form__msg-content-container--scrollable .msg-form__contenteditable"),
            ("css", "div[contenteditable='true'][role='textbox']")
        ]

        compose_box = None
        for selector_type, selector in compose_selectors:
            try:
                if selector_type == "xpath":
                    compose_box = WebDriverWait(self.driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    compose_box = WebDriverWait(self.driver, 8).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )

                if compose_box:
                    logger.info(f"✅ Message compose area found using: {selector}")
                    break
            except (TimeoutException, NoSuchElementException):
                continue

        if not compose_box:
            logger.error("❌ Could not find message compose area")
            return False

        # Type the message
        try:
            compose_box.click()
            self.human_delay(0.5, 1)
            compose_box.clear()

            # Type message character by character
            for char in message:
                compose_box.send_keys(char)
                time.sleep(random.uniform(0.05, 0.15))

            logger.info("✅ Message typed successfully")
            self.human_delay(1, 2)
        except Exception as e:
            logger.error(f"❌ Failed to type message: {e}")
            return False

        # Send the message
        send_button_selectors = [
            ("css", "button.msg-form__send-button[type='submit']"),
            ("css", "button[data-control-name='send_message']"),
            ("xpath", "//button[@type='submit' and .//span[text()='Send']]"),
            ("xpath", "//button[contains(@aria-label, 'Send') and @type='submit']"),
            ("css", "button[aria-label*='Send message']")
        ]

        send_btn = None
        for selector_type, selector in send_button_selectors:
            try:
                if selector_type == "xpath":
                    send_btn = WebDriverWait(self.driver, 6).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    send_btn = WebDriverWait(self.driver, 6).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )

                if send_btn and send_btn.is_enabled():
                    logger.info(f"✅ Send button found using: {selector}")
                    break
            except (TimeoutException, NoSuchElementException):
                continue

        if not send_btn or not send_btn.is_enabled():
            logger.error("❌ Send button not found or not enabled")
            return False

        try:
            if self.safe_click(send_btn):
                logger.info(f"🎉 Message sent successfully to {name}!")
                self.human_delay(1, 2)
                return True
            else:
                logger.error("❌ Failed to click Send button")
                return False
        except Exception as e:
            logger.error(f"❌ Error sending message: {e}")
            return False
            
    def send_direct_message(driver, message, name):
        """Enhanced direct message function with robust button detection"""
        logger.info(f"🔍 Attempting to locate Message button for {name}...")
        
        # Multiple selector strategies for the Message button
        message_button_selectors = [
            # Primary selectors (most reliable)
            ("css", "button[aria-label*='Message'][class*='artdeco-button']"),
            ("css", "button.artdeco-button--primary[aria-label*='Message']"),
            ("xpath", "//button[contains(@aria-label, 'Message') and contains(@class, 'artdeco-button')]"),
            
            # Secondary selectors based on common patterns
            ("xpath", "//button[.//span[text()='Message']]"),
            ("css", "button[data-control-name*='message']"),
            ("css", "button.pvs-profile-actions__action[aria-label*='Message']"),
            
            # Fallback selectors
            ("css", "button[aria-label*='Message']"),
            ("xpath", "//button[contains(text(), 'Message')]"),
            ("xpath", "//span[text()='Message']/parent::button")
        ]
        
        msg_btn = None
        for selector_type, selector in message_button_selectors:
            try:
                if selector_type == "xpath":
                    msg_btn = WebDriverWait(driver, 6).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    msg_btn = WebDriverWait(driver, 6).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                
                # Verify button is actually visible and enabled
                if msg_btn and msg_btn.is_displayed() and msg_btn.is_enabled():
                    logger.info(f"✅ Message button found using: {selector}")
                    break
                else:
                    msg_btn = None
                    
            except (TimeoutException, NoSuchElementException):
                continue
        
        if not msg_btn:
            logger.info("❌ No Message button found - user may not be a 1st degree connection")
            return False
        
        # Scroll button into view and click
        try:
            driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", msg_btn)
            human_delay(1, 2)
            
            # Try clicking the button
            if not safe_click(driver, msg_btn):
                # Alternative click method
                ActionChains(driver).move_to_element(msg_btn).click().perform()
                
            logger.info("✅ Message button clicked successfully")
            human_delay(2, 3)
            
        except Exception as e:
            logger.error(f"❌ Failed to click Message button: {e}")
            return False
        
        # Enhanced message composition with multiple selectors
        compose_selectors = [
            ("css", ".msg-form__contenteditable"),
            ("css", "[data-test-id='message-composer-input']"),
            ("css", "div[role='textbox'][contenteditable='true']"),
            ("xpath", "//textarea[@aria-label='Write a message…']"),
            ("css", ".msg-form__msg-content-container--scrollable .msg-form__contenteditable"),
            ("css", "div[contenteditable='true'][role='textbox']")
        ]
        
        compose_box = None
        for selector_type, selector in compose_selectors:
            try:
                if selector_type == "xpath":
                    compose_box = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    compose_box = WebDriverWait(driver, 8).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                
                if compose_box:
                    logger.info(f"✅ Message compose area found using: {selector}")
                    break
                    
            except (TimeoutException, NoSuchElementException):
                continue
        
        if not compose_box:
            logger.error("❌ Could not find message compose area")
            return False
        
        # Type the message with human-like behavior
        try:
            compose_box.click()
            human_delay(0.5, 1)
            compose_box.clear()
            
            # Type message character by character
            for char in message:
                compose_box.send_keys(char)
                time.sleep(random.uniform(0.05, 0.15))
                
            logger.info("✅ Message typed successfully")
            human_delay(1, 2)
            
        except Exception as e:
            logger.error(f"❌ Failed to type message: {e}")
            return False
        
        # Enhanced Send button detection
        send_button_selectors = [
            ("css", "button.msg-form__send-button[type='submit']"),
            ("css", "button[data-control-name='send_message']"),
            ("xpath", "//button[@type='submit' and .//span[text()='Send']]"),
            ("xpath", "//button[contains(@aria-label, 'Send') and @type='submit']"),
            ("css", "button[aria-label*='Send message']")
        ]
        
        send_btn = None
        for selector_type, selector in send_button_selectors:
            try:
                if selector_type == "xpath":
                    send_btn = WebDriverWait(driver, 6).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    send_btn = WebDriverWait(driver, 6).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                
                if send_btn and send_btn.is_enabled():
                    logger.info(f"✅ Send button found using: {selector}")
                    break
                    
            except (TimeoutException, NoSuchElementException):
                continue
        
        if not send_btn or not send_btn.is_enabled():
            logger.error("❌ Send button not found or not enabled")
            return False
        
        # Send the message
        try:
            if safe_click(driver, send_btn):
                logger.info(f"🎉 Message sent successfully to {name}!")
                human_delay(1, 2)
                return True
            else:
                logger.error("❌ Failed to click Send button")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error sending message: {e}")
            return False
    def process_inbox_replies(self, max_replies=5):
        """Process unread messages with improved reliability."""
        logger.info("🤖 Starting AI inbox processing...")
        results = []
        
        if not self.ensure_linkedin_session():
            return {"success": False, "error": "Login failed"}
        
        if not self.navigate_to_messaging():
            return {"success": False, "error": "Messaging navigation failed"}
        
        try:
            # Find unread conversations using more reliable selector
            unread_selector = (
                "li.msg-conversations-container__conversation-list-item:has(.notification-badge--show), "  # New UI
                "li.conversation-list-item:has(.unread)"  # Old UI
            )
            unread_items = WebDriverWait(self.driver, 10).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, unread_selector))
            )
            logger.info(f"Found {len(unread_items)} unread conversations")
            
            for idx, item in enumerate(unread_items[:max_replies]):
                try:
                    # Extract participant name with more reliable selector
                    name_elem = item.find_element(
                        By.CSS_SELECTOR,
                        ".msg-conversation-listitem__participant-names, .conversation-list-item__participant-names"
                    )
                    name = name_elem.text.strip()
                    
                    logger.info(f"Processing conversation with {name} ({idx+1}/{len(unread_items)})")
                    
                    # Open conversation using JavaScript click for reliability
                    self.driver.execute_script("arguments[0].click();", item)
                    self.human_delay(2, 3)
                    
                    # Wait for conversation to load
                    WebDriverWait(self.driver, 10).until(
                        EC.any_of(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div.msg-s-message-list-content")),
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div.msg-thread"))
                        )
                    )
                    
                    # Get message history
                    history = self.get_conversation_history()
                    
                    if not history:
                        logger.warning("No messages found, skipping")
                        results.append({"name": name, "status": "skipped", "reason": "empty history"})
                        self.navigate_to_messaging()
                        continue
                    
                    # Check if last message is from user
                    if history and history[-1]["sender"] == "You":
                        logger.info("Last message was from user, skipping")
                        results.append({"name": name, "status": "skipped", "reason": "already replied"})
                        self.navigate_to_messaging()
                        continue
                    
                    # Generate AI response
                    ai_reply = self.generate_ai_chat_response(history)
                    
                    # Send response
                    if self.send_chat_message(ai_reply):
                        logger.info(f"✅ Replied to {name}")
                        results.append({"name": name, "status": "replied", "message": ai_reply})
                    else:
                        logger.error(f"❌ Failed to reply to {name}")
                        results.append({"name": name, "status": "failed", "reason": "send error"})
                    
                    # Return to inbox
                    self.navigate_to_messaging()
                    self.human_delay(2, 4)
                    
                except Exception as e:
                    logger.error(f"Error processing conversation: {e}")
                    results.append({"name": f"Unknown{idx}", "status": "error", "reason": str(e)})
                    try:
                        self.navigate_to_messaging()
                    except:
                        self.driver.refresh()
            
            return {"success": True, "results": results}
        
        except Exception as e:
            logger.error(f"Inbox processing failed: {e}")
            return {"success": False, "error": str(e)}

    def send_connection_request_with_note(self, message, name):
        if not self.driver:             # session lost? rebuild once, otherwise continue
            self.setup_driver()
            self.login()
        """Send connection request with personalized note"""
        logger.info(f"🤝 Attempting connection request with note to {name}...")
        
        # Find Connect button
        connect_selectors = [
            "button.artdeco-button.artdeco-button--2.artdeco-button--primary[aria-label*='Connect']",
            "//button[contains(@aria-label, 'Connect') and contains(@class, 'artdeco-button--primary')]",
            "//button[.//span[text()='Connect']]"
        ]
        
        connect_button = None
        for selector in connect_selectors:
            try:
                if selector.startswith("//"):
                    connect_button = self.wait.until(EC.element_to_be_clickable((By.XPATH, selector)))
                else:
                    connect_button = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                break
            except TimeoutException:
                continue
                
        if not connect_button:
            logger.error("❌ Connect button not found")
            return False
            
        # Click Connect
        if not self.safe_click(connect_button):
            logger.error("❌ Failed to click Connect button")
            return False
            
        logger.info("✅ Connect button clicked")
        self.human_delay(2, 3)
        
        try:
            # Try to add a note first
            try:
                add_note_button = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Add a note')]"))
                )
                add_note_button.click()
                time.sleep(1)
                
                # Find note text area
                note_area = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "textarea[name='message'], #custom-message"))
                )
                
                # Type the note
                for char in message:
                    note_area.send_keys(char)
                    time.sleep(random.uniform(0.05, 0.15))
                
                logger.info(f"✅ Added personalized note for {name}")
                
            except TimeoutException:
                logger.info(f"ℹ️ No 'Add a note' option for {name} - sending without note")
            
            # Click send (catch any of the variants)
            for xpath in [
                "//button[normalize-space()='Send now']",
                "//button[normalize-space()='Send']",
                "//button[contains(@aria-label,'Send')]"
            ]:
                try:
                    btn = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, xpath))
                    )
                    btn.click()
                    break
                except TimeoutException:
                    continue
            else:
                logger.error(f"❌ Could not find send button for {name}")
                return False
            
            # Wait for success confirmation
            try:
                WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((
                        By.XPATH,
                        "//button[normalize-space()='Pending'] | //div[contains(text(), 'Invitation sent')]"
                    ))
                )
                logger.info(f"✅ Connection request sent to {name}!")
                self.human_delay(2, 4)
                return True
            except TimeoutException:
                return False
                
        except Exception as e:
            logger.error(f"❌ Error sending connection request: {e}")
            return False

    def search_profiles(self, keywords, location="", industry="", max_invites=20):
        """Search profiles via keyword and send connection requests from within class context."""
        logger.info(f"🔍 Searching for: {keywords}")
        if not self.login():
            logger.error("❌ Login failed - cannot proceed with keyword search")
            return 0

        url = (
            "https://www.linkedin.com/search/results/people/"
            f"?keywords={quote_plus(keywords)}&origin=GLOBAL_SEARCH_HEADER"
        )
        self.driver.get(url)
        time.sleep(3)

        sent_count = 0
        page_loops = 0
        total_attempts = 0

        while sent_count < max_invites and page_loops < 10:
            logger.info(f"📊 Status: {sent_count}/{max_invites} invitations sent (attempts: {total_attempts})")

            connect_buttons = self.find_connect_buttons_enhanced()
            if not connect_buttons:
                logger.info("No connect buttons found on this page.")
                if not self.go_to_next_page():
                    break
                page_loops += 1
                continue

            for button in connect_buttons:
                if sent_count >= max_invites:
                    logger.info(f"🎯 Target reached: {sent_count}/{max_invites}")
                    return sent_count

                try:
                    name = self.extract_name_from_search_result(button)
                except Exception:
                    name = "Professional"

                logger.info(f"🔄 Attempting to connect with {name}")

                success = self._attempt_connection(button, name)
                total_attempts += 1

                if success:
                    sent_count += 1
                    logger.info(f"✅ Invitation sent to {name} ({sent_count}/{max_invites})")
                    self.human_delay(2, 4)
                else:
                    logger.info(f"❌ Failed to send invitation to {name}")
                    self.human_delay(1, 2)

            if not self.go_to_next_page():
                logger.info("No more pages to navigate.")
                break

            page_loops += 1
            self.human_delay(1, 3)

        logger.info(f"🏁 Finished: {sent_count}/{max_invites} invitations sent ({total_attempts} total attempts)")
        return sent_count
    
    # --- Start of New AI Response Feature ---

    def navigate_to_messaging(self):
        """Navigates to the LinkedIn messaging page with improved reliability."""
        logger.info("Navigating to LinkedIn messaging...")
        try:
            self.driver.get("https://www.linkedin.com/messaging")
            # Wait for either new or old messaging UI
            WebDriverWait(self.driver, 15).until(
                EC.any_of(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "ul.msg-conversations-container__conversations-list")),  # New UI
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.msg-threads"))  # Old UI
                )
            )
            logger.info("Successfully loaded messaging page.")
            self.human_delay(2, 3)
            return True
        except TimeoutException:
            logger.error("Failed to load messaging page in time.")
            return False
        except Exception as e:
            logger.error(f"Navigation error: {e}")
            return False

    def get_conversation_history(self):
        """Robust conversation history extraction for both UI versions."""
        logger.info("Extracting conversation history...")
        conversation = []
        try:
            # Wait for message container (supports both UI versions)
            self.wait.until(
                EC.any_of(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.msg-s-message-list-content")),  # New UI
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.msg-thread"))  # Old UI
                )
            )
            
            # New UI extraction
            if self.driver.find_elements(By.CSS_SELECTOR, "div.msg-s-message-list-content"):
                message_elements = self.driver.find_elements(By.CSS_SELECTOR, "li.msg-s-message-list__event")
                for msg in message_elements:
                    try:
                        # Extract sender name
                        try:
                            sender = msg.find_element(By.CSS_SELECTOR, ".msg-s-message-group__name").text.strip()
                        except:
                            sender = "You"
                        
                        # Extract message text
                        try:
                            content = msg.find_element(By.CSS_SELECTOR, ".msg-s-event-listitem__body").text.strip()
                        except:
                            content = ""
                        
                        if content:
                            conversation.append({"sender": sender, "message": content})
                            
                    except Exception as e:
                        logger.debug(f"Skipping message: {e}")
            
            # Old UI extraction
            elif self.driver.find_elements(By.CSS_SELECTOR, "div.msg-thread"):
                message_elements = self.driver.find_elements(By.CSS_SELECTOR, "div.msg-s-event-listitem")
                for msg in message_elements:
                    try:
                        # Extract sender
                        try:
                            sender = msg.find_element(By.CSS_SELECTOR, "span.msg-s-message-group__name").text.strip()
                        except:
                            if "msg-s-event-listitem__self" in msg.get_attribute("class"):
                                sender = "You"
                            else:
                                sender = "Unknown"
                        
                        # Extract content
                        try:
                            content = msg.find_element(By.CSS_SELECTOR, "p").text.strip()
                        except:
                            content = ""
                        
                        if content:
                            conversation.append({"sender": sender, "message": content})
                            
                    except Exception as e:
                        logger.debug(f"Skipping message: {e}")
            
            logger.info(f"Extracted {len(conversation)} messages")
            # Return messages in chronological order (oldest first)
            return conversation
            
        except Exception as e:
            logger.error(f"History extraction failed: {e}")
            return []



    def generate_ai_chat_response(self, conversation_history, user_persona="a helpful professional assistant"):
        """
        Generates a contextual response to a conversation using Gemini AI.
        """
        if not self.model:
            logger.error("AI model is not initialized. Cannot generate response.")
            return "Sorry, I am unable to generate a response at this time."

        if not conversation_history:
            logger.warning("Conversation history is empty. Cannot generate a contextual response.")
            return "Could you please provide more context?"
            
        logger.info("Generating AI response for the chat...")

        # Format the conversation history for the AI prompt
        formatted_history = "\n".join([f"{msg['sender']}: {msg['message']}" for msg in conversation_history])
        
        # Get the name of the other person (the last sender who is not 'You')
        other_person_name = "there"
        for msg in reversed(conversation_history):
            if msg['sender'] != 'You':
                other_person_name = msg['sender'].split()[0] # Get first name
                break

        prompt = f"""Craft a professional LinkedIn reply based on this conversation. Guidelines:
1. Be concise (1-2 sentences max)
2. Match the sender's tone (formal/casual)
3. Address unread messages specifically
4. Never use markdown or special formatting
5. Respond naturally to questions
6. Sign with just your first name

Recent messages:
{formatted_history}

Response:"""
        try:
            response = self.model.generate_content(prompt)
            ai_message = response.text.strip()
            # Clean up any AI-added labels
            ai_message = re.sub(r'^(Your Response:|Response:)\s*', '', ai_message, flags=re.IGNORECASE)
            logger.info(f"AI generated response: {ai_message}")
            return ai_message
        except Exception as e:
            logger.error(f"AI response generation failed: {e}")
            return "I appreciate you reaching out. Let me review this and get back to you shortly."

    def send_chat_message(self, message):
        """
        Types and sends a message in the currently active chat window.
        """
        logger.info(f"Sending message: '{message[:50]}...'")
        try:
            # Wait for message box to be ready
            message_box_selector = "div.msg-form__contenteditable[role='textbox']"
            message_box = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, message_box_selector))
            )
            
            # Wait for any previous messages to clear
            self.human_delay(1, 2)
            
            # Clear any existing text
            self.driver.execute_script("arguments[0].innerText = '';", message_box)
            message_box.send_keys(" ")  # Trigger any required events
            self.human_delay(0.5, 1)
            
            # Type message
            self.type_like_human(message_box, message)
            self.human_delay(1, 2)
            
            # Find and click the send button
            send_button = self.driver.find_element(
                By.CSS_SELECTOR, 
                "button.msg-form__send-button[type='submit'], button.msg-form-send-button"
            )
            
            # Ensure button is enabled
            if send_button.is_enabled():
                self.safe_click(send_button)
                logger.info("Message sent successfully.")
                self.human_delay(2, 4)
                return True
            else:
                logger.error("Send button is disabled.")
                return False
                
        except TimeoutException:
            logger.error("Message input box not found or not interactable.")
            return False
        except NoSuchElementException:
            logger.error("Send button not found.")
            return False
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
            return False

    def ai_respond_to_conversation(self, conversation_name):
        """
        Orchestrates the process of reading a conversation and sending an AI-generated reply.
        
        :param conversation_name: The name of the person in the conversation to open.
        """
        logger.info(f"Starting AI response process for conversation with {conversation_name}.")
        
        # 1. Navigate to messaging
        if not self.navigate_to_messaging():
            return

        # 2. Select the specific conversation
        try:
            logger.info(f"Searching for conversation with '{conversation_name}'...")
            # More robust XPath to find the conversation list item
            conversation_xpath = f"//h3[contains(@class, 'msg-conversation-listitem__participant-names') and contains(normalize-space(), '{conversation_name}')]/ancestor::li[contains(@class, 'msg-conversation-listitem')]"
            conversation_element = self.wait.until(
                EC.element_to_be_clickable((By.XPATH, conversation_xpath))
            )
            self.safe_click(conversation_element)
            logger.info(f"Opened conversation with {conversation_name}.")
            self.human_delay(2, 3)
        except TimeoutException:
            logger.error(f"Could not find or click on conversation with '{conversation_name}'.")
            return
        except Exception as e:
            logger.error(f"An error occurred while opening the conversation: {e}")
            return

        # 3. Read the conversation history
        history = self.get_conversation_history()
        if not history:
            logger.warning("Could not read conversation history. Aborting.")
            return
            
        # Check if the last message is from 'You'
        if history and history[-1]['sender'] == 'You':
            logger.info("The last message was already sent by you. No response needed.")
            return

        # 4. Generate AI response
        ai_response = self.generate_ai_chat_response(history)

        # 5. Send the response
        if ai_response:
            self.send_chat_message(ai_response)
        else:
            logger.error("AI failed to generate a response. Message not sent.")
            
    # --- End of New AI Response Feature ---
        
        
    def go_to_next_page(self):
        try:
            next_btn = WebDriverWait(self.driver, 5).until(
                EC.element_to_be_clickable((
                    By.XPATH, 
                    "//button[@aria-label='Next' and not(@disabled)] | //a[@aria-label='Next']"
                ))
            )
                
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", next_btn
            )
            next_btn.click()
            time.sleep(2)
            return True
                
        except TimeoutException:
            return False
        except Exception as e:
            logger.warning(f"Next page navigation error: {e}")
            return False
    def find_element_safe(self, selectors, timeout=10):
        """Enhanced element finding with multiple selectors"""
        for selector_type, selector in selectors:
            try:
                if selector_type == "xpath":
                    element = WebDriverWait(self.driver, timeout).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                else:
                    element = WebDriverWait(self.driver, timeout).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                    )
                return element
            except TimeoutException:
                continue
        return None
    
    def find_connect_buttons_enhanced(self):
        """Enhanced button detection with multiple strategies"""
        selectors = [
            "//button[contains(text(), 'Connect') and not(contains(@class, 'artdeco-button--disabled'))]",
            "//button[.//span[text()='Connect'] and not(contains(@class, 'disabled'))]",
            "//button[contains(@aria-label, 'Connect') and not(@disabled)]"
        ]
        
        buttons = []
        for selector in selectors:
            try:
                found_buttons = self.driver.find_elements(By.XPATH, selector)
                # Filter out already processed buttons
                for btn in found_buttons:
                    if btn.is_displayed() and btn.is_enabled():
                        buttons.append(btn)
            except Exception as e:
                logger.debug(f"Selector failed: {selector}, Error: {e}")
        
        # Remove duplicates
        unique_buttons = list(dict.fromkeys(buttons))
        logger.info(f"Found {len(unique_buttons)} available connect buttons")
        return unique_buttons
    
    def click_connect_and_validate(self, button):
        """Scrolls to and clicks the Connect button, handles the modal, and returns True if the invite went through"""
        try:
            # Scroll & click via JavaScript (most reliable)
            self.driver.execute_script("arguments[0].scrollIntoView(true);", button)
            self.driver.execute_script("arguments[0].click();", button)
            
            # Give a brief pause before modal appears
            time.sleep(1)
            
            return self.handle_connect_modal()
            
        except Exception as e:
            logger.error(f"Error clicking connect button: {e}")
            return False
            
    def _extract_name_from_button(self, button):
        """Extract name from connect button context"""
        try:
            parent = button.find_element(By.XPATH, "./ancestor::div[contains(@class, 'entity-result')]")
            name_elem = parent.find_element(By.CSS_SELECTOR, "[aria-hidden='true']")
            return name_elem.text.strip()
        except Exception:
            return "Professional"
        

    def extract_name_from_search_result(self, button):
        """Extract name from search result card"""
        try:
            # Find the parent container
            parent = button.find_element(By.XPATH, "./ancestor::div[contains(@class, 'search-result__info')]")
            name_elem = parent.find_element(By.CSS_SELECTOR, ".search-result__result-link")
            return name_elem.text.strip()
        except:
            return "Professional"


    def handle_connect_modal_safe(self, name):
        """Handle connection modal with error recovery"""
        try:
            # Try to add note first
            try:
                add_note_btn = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Add a note')]"))
                )
                add_note_btn.click()
                time.sleep(1)
                
                # Type note
                note_area = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "textarea[name='message']"))
                )
                note_text = f"Hi {name.split()[0]}, I'd love to connect and learn about your professional journey!"
                note_area.send_keys(note_text)
                time.sleep(1)
                
            except TimeoutException:
                logger.info(f"No note option for {name}")
            
            # Click send
            send_selectors = [
                "//button[normalize-space()='Send now']",
                "//button[normalize-space()='Send']",
                "//button[contains(@aria-label,'Send')]"
            ]
            
            for selector in send_selectors:
                try:
                    send_btn = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    send_btn.click()
                    
                    # Wait for confirmation
                    WebDriverWait(self.driver, 5).until(
                        EC.presence_of_element_located((
                            By.XPATH, 
                            "//button[normalize-space()='Pending'] | //div[contains(text(), 'Invitation sent')]"
                        ))
                    )
                    return True
                    
                except TimeoutException:
                    continue
            
            return False
            
        except Exception as e:
            logger.error(f"Modal handling error for {name}: {e}")
            return False
        
    def extract_name_from_search_result(self, button):
        """Extract name from search result card"""
        try:
            # Find the parent container
            parent = button.find_element(By.XPATH, "./ancestor::div[contains(@class, 'entity-result')]")
            name_elem = parent.find_element(By.CSS_SELECTOR, "[aria-hidden='true']")
            return name_elem.text.strip()
        except Exception:
            return "Professional"

    def human_delay(self, min_seconds=1, max_seconds=3):
        """Add human-like delays"""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)

    def safe_click(self, element):
        """Safely click an element with fallback via ActionChains"""
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({behavior:'smooth',block:'center'});", element)
            time.sleep(random.uniform(0.5, 1.5))
            element.click()
            return True
        except (ElementClickInterceptedException, ElementNotInteractableException):
            try:
                ActionChains(self.driver).move_to_element(element).pause(0.5).click().perform()
                return True
            except Exception as e:
                logger.warning(f"Click fallback failed: {e}")
                return False
        except Exception as e:
            logger.warning(f"Click failed: {e}")
            return False
            
    def close(self):
        """Clean up resources"""
        try:
            if self.driver:
                self.driver.quit()
                
            # Clean up temporary profile directory
            self._cleanup_profile()
                
        except Exception as e:
            logger.warning(f"Cleanup warning: {e}")
    
    def _healthy(self):
        try:
            self.driver.title                # simple ping
            return True
        except Exception:
            return False

    def _ensure(self):
        if self._healthy():
            return
        self.close()
        self.setup_driver()
        self.login()                         # silent re-login

            
    def __del__(self):
        """Cleanup when object is destroyed"""
        self.close()