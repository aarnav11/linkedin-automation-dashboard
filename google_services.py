import os
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
SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/userinfo.email',  # <-- ADDED
    'https://www.googleapis.com/auth/userinfo.profile' # <-- ADDED
]

def create_google_auth_flow(redirect_uri: str) -> Flow:
    """
    Creates and returns a Google OAuth Flow object.
    """
    flow = Flow.from_client_secrets_file(
        _CLIENT_SECRET_FILE,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    
    # This tells Google that we want a refresh_token
    #flow.authorization_url(access_type='offline', prompt='consent')
    
    return flow

def build_service_from_user(user: User, service_name: str, service_version: str) -> Resource | None:
    """
    Builds and returns an authenticated Google API service client for a user.
    
    Returns None if the user is not authenticated with Google.
    """
    if not user.google_refresh_token:
        print(f"User {user.email} does not have a Google refresh token.")
        return None

    try:
        # Create credentials from the stored refresh token
        credentials = google.oauth2.credentials.Credentials(
            token=None,  # No access token needed; it will be refreshed
            refresh_token=user.google_refresh_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=None,  # Will be loaded from client_secret.json
            client_secret=None, # Will be loaded from client_secret.json
            scopes=user.google_scopes
        )
        
        # We need to load client_id and client_secret for the refresh to work
        # This is a bit of a workaround for the Credentials object
        import json
        with open(_CLIENT_SECRET_FILE, 'r') as f:
            secrets = json.load(f).get('web')
            credentials.client_id = secrets.get('client_id')
            credentials.client_secret = secrets.get('client_secret')

        # Build the service
        service = build(
            service_name,
            service_version,
            credentials=credentials
        )
        
        # The credentials object will automatically refresh the access token
        # if it's expired or missing, using the refresh token.
        
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