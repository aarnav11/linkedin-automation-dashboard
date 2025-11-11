import os
import json
import google.oauth2.credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build, Resource
from models import User  # We'll need the User model to build services
import base64
import uuid
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
# This is the file you downloaded from Google Cloud Console
_BASE_DIR = os.path.abspath(os.path.dirname(__file__))
_CLIENT_SECRET_FILE = os.path.join(_BASE_DIR, "client_secret.json")

# --- THIS LIST IS NOW UPDATED ---
def load_google_config():
    """
    Tries to load Google credentials from:
    1. AWS Environment Variable (GOOGLE_CREDENTIALS_JSON)
    2. Secret File on Disk (Render / Localhost)
    """
    # Priority 1: Check for Environment Variable (AWS Strategy)
    env_config = os.environ.get('GOOGLE_CREDENTIALS_JSON')
    if env_config:
        try:
            return json.loads(env_config)
        except json.JSONDecodeError:
            print("Error: GOOGLE_CREDENTIALS_JSON contains invalid JSON.")
    
    # Priority 2: Check for File (Render/Local Strategy)
    if os.path.exists(_CLIENT_SECRET_FILE):
        try:
            with open(_CLIENT_SECRET_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error reading client_secret.json: {e}")

    # Fallback: Return None if neither exists
    return None

_GOOGLE_CONFIG = load_google_config()

SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/userinfo.email', 
    'https://www.googleapis.com/auth/userinfo.profile' 
]

def create_google_auth_flow(redirect_uri: str) -> Flow:
    if not _GOOGLE_CONFIG:
        raise ValueError("Google Credentials not found. Check GOOGLE_CREDENTIALS_JSON env var or client_secret.json file.")

    # Standardize to use from_client_config (works for both file content and env var content)
    flow = Flow.from_client_config(
        _GOOGLE_CONFIG,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    
    return flow

def build_service_from_user(user: User, service_name: str, service_version: str) -> Resource | None:
    if not user.google_refresh_token:
        print(f"User {user.email} does not have a Google refresh token.")
        return None
    
    if not _GOOGLE_CONFIG:
         print("Cannot build service: Google Configuration is missing.")
         return None

    try:
        # Safely extract credentials from the loaded dictionary
        # Support both 'web' and 'installed' formats just in case
        config_root = _GOOGLE_CONFIG.get('web') or _GOOGLE_CONFIG.get('installed')
        
        if not config_root:
            print("Error: Invalid client config format (missing 'web' or 'installed' key)")
            return None

        credentials = google.oauth2.credentials.Credentials(
            token=None, 
            refresh_token=user.google_refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=config_root.get('client_id'),
            client_secret=config_root.get('client_secret'),
            scopes=user.google_scopes
        )
        
        service = build(
            service_name,
            service_version,
            credentials=credentials
        )
        
        return service
        
    except Exception as e:
        print(f"Error building Google service for user {user.email}: {e}")
        return None
    
def send_email(user: User, to_email: str, subject: str, body: str) -> dict:
    """
    Sends an email on behalf of the user using their Gmail account.
    
    """
    try:
        service = build_service_from_user(user, 'gmail', 'v1')
        if not service:
            raise Exception("Failed to build Gmail service.")

        message = MIMEText(body)
        message['to'] = to_email
        message['from'] = 'me'  # 'me' refers to the authenticated user
        message['subject'] = subject

        # Encode the message
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': raw_message}
        
        send_result = service.users().messages().send(
            userId='me',
            body=create_message
        ).execute()
        
        print(f"Email sent successfully to {to_email}. ID: {send_result.get('id')}")
        return send_result

    except Exception as e:
        print(f"Error sending email: {e}")
        raise e


# === CALENDAR FUNCTIONS ===

def find_free_slots(user: User, duration_minutes: int = 30, days_ahead: int = 7) -> list:
    """
    Finds available time slots on the user's primary calendar.
    
    """
    try:
        service = build_service_from_user(user, 'calendar', 'v3')
        if not service:
            raise Exception("Failed to build Calendar service.")
            
        # Set time range
        now_utc = datetime.now(timezone.utc)
        time_min_obj = now_utc.replace(hour=9, minute=0, second=0, microsecond=0) # Start today at 9am
        time_max_obj = (now_utc + timedelta(days=days_ahead)).replace(hour=17, minute=0, second=0, microsecond=0) # End in 7 days at 5pm

        time_min = time_min_obj.isoformat()
        time_max = time_max_obj.isoformat()

        # Call the FreeBusy API
        freebusy_query = {
            "timeMin": time_min,
            "timeMax": time_max,
            "timeZone": "UTC",
            "items": [{"id": "primary"}]
        }
        
        results = service.freebusy().query(body=freebusy_query).execute()
        
        busy_slots = results.get('calendars', {}).get('primary', {}).get('busy', [])
        
        # --- Simple Slot Finding Logic ---
        available_slots = []
        slot_start = time_min_obj
        duration = timedelta(minutes=duration_minutes)
        
        while slot_start + duration <= time_max_obj:
            # Only check slots within "working hours" (e.g., 9-5)
            if 9 <= slot_start.hour < 17:
                is_busy = False
                slot_end = slot_start + duration
                
                # Check against busy slots
                for busy in busy_slots:
                    busy_start = datetime.fromisoformat(busy['start'])
                    busy_end = datetime.fromisoformat(busy['end'])
                    
                    # Check for overlap
                    if max(slot_start, busy_start) < min(slot_end, busy_end):
                        is_busy = True
                        break
                
                if not is_busy:
                    available_slots.append(slot_start)
                    
            # Move to the next slot (e.g., 30 min increments)
            slot_start += duration
            
            # If end of day, jump to next morning
            if slot_start.hour >= 17:
                slot_start = slot_start.replace(hour=9, minute=0) + timedelta(days=1)

        # Return the first 5 available slots
        return available_slots[:5]

    except Exception as e:
        print(f"Error finding free slots: {e}")
        return []


def create_event(user: User, summary: str, start_dt: datetime, end_dt: datetime, attendee_email: str) -> dict:
    """
    Creates a new event on the user's primary calendar with a Google Meet link
    and sends an invitation to the attendee.
    
    """
    try:
        service = build_service_from_user(user, 'calendar', 'v3')
        if not service:
            raise Exception("Failed to build Calendar service.")
            
        event = {
            'summary': summary,
            'start': {
                'dateTime': start_dt.isoformat(),
                'timeZone': 'UTC',
            },
            'end': {
                'dateTime': end_dt.isoformat(),
                'timeZone': 'UTC',
            },
            'attendees': [
                {'email': attendee_email},
                {'email': 'me'} # 'me' refers to the user
            ],
            'conferenceData': {
                'createRequest': {
                    'requestId': str(uuid.uuid4()),
                    'conferenceSolutionKey': {
                        'type': 'hangoutsMeet'
                    }
                }
            },
            'reminders': {
                'useDefault': True,
            },
        }
        
        created_event = service.events().insert(
            calendarId='primary',
            body=event,
            conferenceDataVersion=1, # This is required to create the Meet link
            sendUpdates='all' # This sends the invite to the attendee
        ).execute()
        
        print(f"Event created successfully. ID: {created_event.get('id')}")
        return created_event

    except Exception as e:
        print(f"Error creating event: {e}")
        raise e