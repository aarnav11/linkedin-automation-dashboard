import os
import requests
import json
from datetime import datetime, timedelta
from models import User

# Load from environment variables
HUBSPOT_CLIENT_ID = os.environ.get('HUBSPOT_CLIENT_ID')
HUBSPOT_CLIENT_SECRET = os.environ.get('HUBSPOT_CLIENT_SECRET')
# This must match the Redirect URI set in your HubSpot Developer App
HUBSPOT_REDIRECT_URI = os.environ.get('HUBSPOT_REDIRECT_URI', 'http://localhost:5000/oauth2callback-hubspot')

BASE_URL = "https://api.hubapi.com"

def get_auth_url():
    """Generates the HubSpot OAuth URL for the user."""
    scopes = 'crm.objects.contacts.write crm.objects.contacts.read'
    return (
        f"https://app.hubspot.com/oauth/authorize"
        f"?client_id={HUBSPOT_CLIENT_ID}"
        f"&scope={scopes}"
        f"&redirect_uri={HUBSPOT_REDIRECT_URI}"
    )

def exchange_code_for_token(code):
    """Exchanges the temp auth code for access/refresh tokens."""
    url = "https://api.hubapi.com/oauth/v1/token"
    data = {
        'grant_type': 'authorization_code',
        'client_id': HUBSPOT_CLIENT_ID,
        'client_secret': HUBSPOT_CLIENT_SECRET,
        'redirect_uri': HUBSPOT_REDIRECT_URI,
        'code': code
    }
    response = requests.post(url, data=data)
    return response.json()

def refresh_access_token(user: User):
    """Refreshes the access token if expired."""
    if not user.hubspot_refresh_token:
        return None

    url = "https://api.hubapi.com/oauth/v1/token"
    data = {
        'grant_type': 'refresh_token',
        'client_id': HUBSPOT_CLIENT_ID,
        'client_secret': HUBSPOT_CLIENT_SECRET,
        'refresh_token': user.hubspot_refresh_token
    }
    
    response = requests.post(url, data=data)
    tokens = response.json()
    
    if 'access_token' in tokens:
        user.hubspot_access_token = tokens['access_token']
        user.hubspot_refresh_token = tokens.get('refresh_token', user.hubspot_refresh_token)
        user.hubspot_token_expires_at = datetime.utcnow() + timedelta(seconds=tokens['expires_in'])
        user.save()
        return user.hubspot_access_token
    
    return None

def create_contact(user: User, email, first_name, last_name, linkedin_url=None, job_title=None, company=None):
    """Creates a contact in HubSpot CRM."""
    token = user.hubspot_access_token
    
    # Check if token needs refresh
    if not token or (user.hubspot_token_expires_at and datetime.utcnow() > user.hubspot_token_expires_at):
        token = refresh_access_token(user)
        
    if not token:
        return {"error": "HubSpot not connected or token expired"}

    url = f"{BASE_URL}/crm/v3/objects/contacts"
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    properties = {
        "email": email,
        "firstname": first_name,
        "lastname": last_name,
        "linkedin_profile": linkedin_url,
        "jobtitle": job_title,
        "company": company,
        "lifecyclestage": "lead" # Mark as Lead as per your requirements
    }
    
    # Filter out None values
    properties = {k: v for k, v in properties.items() if v is not None}

    payload = {"properties": properties}
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        # If 409, contact exists. We could update it, but for now just return the error/status
        return {"error": str(e), "details": response.text}