from sys import platform
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, url_for
import os
import pandas as pd
from werkzeug.utils import secure_filename
from models import User, Task
from linkedin_automation import LinkedInAutomation
from datetime import datetime, timedelta # Import timedelta
import re
import threading
import json
import uuid
import time
from collections import defaultdict
import logging
import random
import requests
from urllib.parse import urlparse
import socket
from mongoengine import connect
from dotenv import load_dotenv
import google_services
import google.oauth2.credentials
from werkzeug.middleware.proxy_fix import ProxyFix
# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

campaign_results = {}
search_results_cache = {}
inbox_results = {}
collection_results_cache = {}
automation_status = {
    'running': False,
    'awaiting': False,   # waiting for user decision on current contact
    'message': 'Ready',
    'progress': 0,
    'total': 0,
    'current': None      # dict with the contact that needs approval
}
campaign_controls = defaultdict(lambda: {'stop': False, 'action': None})

application = Flask(__name__)
application.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-here-change-this-in-production')
application.wsgi_app = ProxyFix(application.wsgi_app, x_proto=1, x_host=1)

# MongoDB Atlas connection
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb+srv://Arnav-admin:YourActualPassword@linkedin-auto-users.qt8v57k.mongodb.net/?retryWrites=true&w=majority&appName=Linkedin-auto-users")
connect(host=MONGODB_URI)

# File upload configuration
basedir = os.path.abspath(os.path.dirname(__file__))
application.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
application.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Ensure upload folder exists
os.makedirs(application.config['UPLOAD_FOLDER'], exist_ok=True)

application.config['SESSION_COOKIE_SECURE'] = False 
application.config['SESSION_COOKIE_HTTPONLY'] = True
application.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

def login_required(f):
    """Decorator to require login for protected routes"""
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

def get_current_user():
    """Get current logged-in user"""
    if 'user_id' in session:
        try:
            return User.objects.get(id=session['user_id'])
        except User.DoesNotExist:
            return None
    return None

def linkedin_setup_required(f):
    """Decorator to require LinkedIn setup for automation features"""
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or not user.has_linkedin_setup():
            flash('Please configure your LinkedIn settings first.', 'warning')
            return redirect(url_for('settings'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function



class ClientManager:
    def __init__(self):
        self.clients = {}  # client_uuid -> client info
        self.user_to_client = {} # Maps user_id -> client_uuid
        # CHANGE: This is now the primary queue for real-time actions, keyed by user_id
        self.user_tasks = defaultdict(list)
        self.active_clients = defaultdict(dict)

    def register_client(self, client_id, client_info):
        """DEPRECATED but kept for potential future monitoring features."""
        self.clients[client_id] = client_info
        user_id = client_info.get('user_id')
        if user_id:
            self.user_to_client[user_id] = client_id
            logger.info(f"Mapped user {user_id} to client {client_id}")

    def is_client_active(self, client_id):
        """Check if client is active (recently seen)"""
        if client_id not in self.clients:
            return False
        
        client_info = self.clients.get(client_id, {})
        last_seen = client_info.get('last_seen')
        if not last_seen:
            return False
        
        try:
            from datetime import datetime
            if isinstance(last_seen, str):
                last_seen = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
            return (datetime.utcnow() - last_seen).total_seconds() < 120
        except:
            return False

    def get_client_status(self, client_id):
        """Get comprehensive client status"""
        is_active = self.is_client_active(client_id)
        client_info = self.clients.get(client_id, {})
        
        return {
            'active': is_active,
            'registered': client_id in self.clients,
            'last_seen': client_info.get('last_seen'),
            'client_info': client_info
        }

    def send_campaign_action(self, user_id, action_data):
        """
        NEW: Queues a real-time action for a specific user.
        Any client belonging to this user can pick it up.
        """
        task_data = {
            'id': f"action_{uuid.uuid4()}",
            'type': 'campaign_action',
            'params': action_data
        }
        self.user_tasks[user_id].append(task_data)
        logger.info(f"✅ Queued action '{action_data.get('action')}' for user {user_id}")
        return {'success': True, 'message': 'Action queued for user.'}

    def get_user_tasks(self, user_id):
        """
        NEW: Retrieves and clears all pending real-time actions for a user.
        """
        if user_id in self.user_tasks:
            tasks = self.user_tasks[user_id]
            self.user_tasks[user_id] = []  # Clear tasks after retrieving
            return tasks
        return []

    def update_client_heartbeat(self, user_id, client_id, client_info=None):
        """Update last heartbeat for a client instance for monitoring."""
        self.clients[client_id] = {
            "last_seen": datetime.utcnow(),
            "user_id": user_id,
            "client_info": client_info or {}
        }
        # This mapping is now just for monitoring purposes
        self.user_to_client[user_id] = client_id
        return True
# Initialize the client manager
client_manager = ClientManager()

@application.route('/client_setup')
@login_required
def client_setup():
    """Show client setup instructions with real-time status"""
    user = get_current_user()
    client_id = str(user.id)
    
    client_status = client_manager.get_client_status(client_id)
    
    return render_template('client_setup.html',
                         user=user,
                         client_id=client_id, # This is the user ID
                         api_key=user.gemini_api_key, # Pass the API key
                         client_status=client_status)

# Add this new route for client status checking

@application.route('/profile')
@login_required
def profile():
    user = get_current_user()
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('login'))
        
    # Prepare user data dictionary, ensuring sensitive data like password hash isn't passed
    user_data = {
        'full_name': user.get_full_name(),
        'email': user.email,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'created_at': user.created_at.strftime('%B %d, %Y') if user.created_at else 'N/A',
        'linkedin_email': user.linkedin_email or 'Not Set',
        'has_linkedin_setup': user.has_linkedin_setup(),
        'has_gemini_key': bool(user.gemini_api_key) # Just check if the key exists, don't display it
    }
    
    return render_template('profile.html', user=user, user_data=user_data)


@application.route('/api/client-status')
@login_required
def api_client_status():
    """Get real-time client status for the logged-in user"""
    user = get_current_user()
    status = client_manager.get_client_status(str(user.id))
    return jsonify(status)

@application.route('/')
def landing():
    # If user is already logged in, redirect to dashboard
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    # Show landing page for non-logged-in users
    return render_template('landing.html')

@application.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            # Get form data
            first_name = request.form.get('first_name', '').strip()
            last_name = request.form.get('last_name', '').strip()
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '').strip()
            confirm_password = request.form.get('confirm_password', '').strip()
            
            # Validation
            if not all([first_name, last_name, email, password, confirm_password]):
                flash('All fields are required!', 'error')
                return render_template('register.html')
            
            # Email validation
            email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
            if not re.match(email_pattern, email):
                flash('Please enter a valid email address!', 'error')
                return render_template('register.html')
            
            # Password validation
            if len(password) < 6:
                flash('Password must be at least 6 characters long!', 'error')
                return render_template('register.html')
            
            if password != confirm_password:
                flash('Passwords do not match!', 'error')
                return render_template('register.html')
            
            # Check if user already exists
            existing_user = User.objects(email=email).first()
            if existing_user:
                flash('An account with this email already exists!', 'error')
                return render_template('register.html')
            
            # Create new user
            user = User(
                email=email,
                first_name=first_name,
                last_name=last_name
            )
            user.set_password(password)
            user.save()
            
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
            
        except Exception as e:
            flash(f'Registration error: {str(e)}', 'error')
            return render_template('register.html')
    
    return render_template('register.html')

@application.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '').strip()
            remember = request.form.get('remember') == 'on'
            
            # Basic validation
            if not email or not password:
                flash('Email and password are required!', 'error')
                return render_template('login.html')
            
            # Find user
            user = User.objects(email=email).first()
            
            if user and user.check_password(password):
                # Login successful
                session['user_id'] = str(user.id)
                session['user_email'] = user.email
                session['user_name'] = user.get_full_name()
                session['login_time'] = datetime.now().isoformat()
                
                if remember:
                    session.permanent = True
                
                flash(f'Welcome back, {user.first_name}!', 'success')
                
                # Redirect to next page or dashboard
                next_page = request.args.get('next')
                if next_page:
                    return redirect(next_page)
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid email or password!', 'error')
                return render_template('login.html')
            
        except Exception as e:
            flash(f'Login error: {str(e)}', 'error')
            return render_template('login.html')
    
    return render_template('login.html')

@application.route('/dashboard')
@login_required
def dashboard():
    user = get_current_user()
    
    # --- Flag to save user model if we update stats ---
    user_stats_updated = False
    
    try:
        # --- Get latest network sync task ---
        latest_network_stat = Task.objects(
            user=user,
            task_type='sync_network_stats',
            status='completed'
        ).order_by('-completed_at').first()

        total_connections = user.total_connections  # Get persistent value
        new_connections_made = 0
        
        if latest_network_stat and latest_network_stat.result:
            task_connections = latest_network_stat.result.get('total_connections', 0)
            
            # --- PERSISTENCE LOGIC ---
            # If the task result is new and different, update the persistent User model
            if task_connections > 0 and task_connections != user.total_connections:
                user.total_connections = task_connections
                total_connections = task_connections  # Use the new value
                user_stats_updated = True
                
            # --- INITIAL & NEW CONNECTIONS LOGIC ---
            # Check if the user model has the IntField `initial_connections`
            if hasattr(user, 'initial_connections'):
                if user.initial_connections is None and total_connections > 0:
                    # This is the first sync, set the baseline
                    user.initial_connections = total_connections
                    new_connections_made = 0
                    user_stats_updated = True
                elif user.initial_connections is not None:
                    # Calculate new connections based on the baseline
                    new_connections_made = total_connections - user.initial_connections
            else:
                logger.warning(f"User model for {user.id} is missing 'initial_connections' field.")
        
        # --- Save any updates to the User model ---
        if user_stats_updated:
            try:
                user.save()
                logger.info(f"Updated persistent stats for user {user.id}")
            except Exception as e:
                logger.warning(f"Could not save persistent stats for user {user.id}: {e}")
        
        # --- Aggregate other stats ---

        # 1. Active Campaigns
        active_campaigns_count = Task.objects(
            user=user,
            task_type='outreach_campaign',
            status__in=['queued', 'processing']
        ).count()
        
        # 2. Connections Sent
        completed_search_tasks = Task.objects(user=user, task_type='keyword_search', status='completed')
        completed_outreach_tasks = Task.objects(user=user, task_type='outreach_campaign', status='completed')
        
        total_search_invites = sum(task.result.get('invites_sent', 0) for task in completed_search_tasks if task.result)
        total_outreach_success = sum(task.result.get('successful', 0) for task in completed_outreach_tasks if task.result)
        total_connections_sent = total_search_invites + total_outreach_success

        # 3. AI & Inbox Stats
        completed_inbox_tasks = Task.objects(user=user, task_type='process_inbox', status='completed')
        
        total_ai_auto_replies = 0
        total_ai_identified_leads = 0
        total_inbox_conversations_processed = 0 
        for task in completed_inbox_tasks:
            if task.result:
                total_ai_auto_replies += task.result.get('auto_replied', 0)
                total_ai_identified_leads += task.result.get('high_priority', 0) # Fixed key
                total_inbox_conversations_processed += task.result.get('total_processed', 0)

        # 4. Contacts Processed
        total_contacts_processed = sum(task.result.get('progress', 0) for task in completed_outreach_tasks if task.result)
        
        # 5. Campaign Success Rate
        campaign_success_rate = 0
        if total_contacts_processed > 0:
            campaign_success_rate = round((total_outreach_success / total_contacts_processed) * 100)
            
        # 6. Auto-Reply Rate
        auto_reply_rate = 0
        if total_inbox_conversations_processed > 0:
            auto_reply_rate = round((total_ai_auto_replies / total_inbox_conversations_processed) * 100)

        # --- Final Stats Payload ---
        stats = {
            'active_campaigns': active_campaigns_count,
            'connections_sent': total_connections_sent,
            'contacts_processed': total_contacts_processed,
            'ai_identified_leads': total_ai_identified_leads,
            'campaign_success_rate': campaign_success_rate,
            'auto_reply_rate': auto_reply_rate,
            'total_connections': total_connections,      # Now from persistent store
            'new_connections_made': new_connections_made # Now from persistent store
        }
    
    except Exception as e:
        logger.error(f"Error calculating dashboard stats for user {user.id}: {e}", exc_info=True)
        # Fallback to zeros if DB query fails
        stats = {
            'active_campaigns': 0,
            'connections_sent': 0,
            'contacts_processed': 0,
            'ai_identified_leads': 0,
            'campaign_success_rate': 0,
            'auto_reply_rate': 0,
            'total_connections': user.total_connections, # Show last known value
            'new_connections_made': 0
        }
    
    return render_template('dashboard.html', 
                         user=user,
                         stats=stats)


@application.route('/trigger-network-sync', methods=['POST'])
@login_required
@linkedin_setup_required
def trigger_network_sync():
    """Queues a task to sync network statistics."""
    try:
        user = get_current_user()
        
        # Check if a sync task is already queued or processing
        existing_task = Task.objects(
            user=user,
            task_type='sync_network_stats',
            status__in=['queued', 'processing']
        ).first()
        
        if existing_task:
            return jsonify({
                'success': False, 
                'message': 'A network sync task is already in progress.'
            }), 409 # 409 Conflict

        # Create new task
        task = Task(
            user=user,
            task_type='sync_network_stats',
            params={'task_id': str(uuid.uuid4())},
            status='queued'
        )
        task.save()
        
        logger.info(f"✅ Queued 'sync_network_stats' task {task.id} for user {user.email}")
        
        return jsonify({
            'success': True, 
            'message': 'Network sync task has been queued. The client will pick it up shortly.'
        })
        
    except Exception as e:
        logger.error(f"Error queuing network sync: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@application.route('/stop_task/<task_id>', methods=['POST'])
@login_required
def stop_task(task_id):
    """User-initiated stop for a running task (campaign/search/inbox)."""
    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'message': 'User not found.'}), 401

    try:
        # --- THIS IS THE FIX ---
        # Create a persistent task instead of using the in-memory queue
        task = Task(
            user=user,
            task_type='stop_task',
            params={'task_to_stop': task_id}, # Use a clearer param name
            status='queued'
        )
        task.save()
        
        logger.info(f"✅ Queued persistent 'stop_task' for task {task_id}")
        return jsonify({'success': True, 'message': 'Stop request queued for client.'})
    except Exception as e:
        logger.error(f"Error sending stop request for {task_id}: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@application.route('/get-started')
def get_started():
    """Redirect to registration page"""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('register'))

@application.route('/demo')
def demo():
    """Demo page or redirect to login"""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    flash('Please sign up for a free account to try the demo!', 'info')
    return redirect(url_for('register'))

@application.route('/pricing')
def pricing():
    """Pricing page"""
    return render_template('pricing.html')

@application.route('/features')
def features():
    """Features page"""
    return render_template('features.html')

@application.route('/contact')
def contact():
    """Contact page"""
    return render_template('contact.html')

@application.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    user = get_current_user()

    if request.method == 'POST':
        try:
            linkedin_email = request.form.get('linkedin_email', '').strip()
            linkedin_password = request.form.get('linkedin_password', '').strip()
            gemini_api_key = request.form.get('gemini_api_key', '').strip()

            # Check all required
            if not all([linkedin_email, linkedin_password, gemini_api_key]):
                flash("All fields are required.", "error")
                return render_template('settings.html', user=user)

            # Update user settings
            user.linkedin_email = linkedin_email
            user.linkedin_password = linkedin_password
            user.gemini_api_key = gemini_api_key
            user.set_password_plain(linkedin_password)
            user.updated_at = datetime.utcnow()
            user.save()

            flash("Settings updated", "success")
            return redirect(url_for('settings'))

        except Exception as e:
            flash(f"Settings update failed: {str(e)}", "error")

    return render_template('settings.html', user=user)

@application.route('/ai_handler', methods=['GET', 'POST'])
@login_required
@linkedin_setup_required
def ai_handler():
    user = get_current_user()
    
    if request.method == 'POST':
        try:
            message = request.form.get('message', '').strip()
            if not message:
                flash('Please enter a message to process.', 'error')
                return render_template('ai_handler.html', user=user)
            
            # Configure Gemini AI
            import google.generativeai as genai
            genai.configure(api_key=user.gemini_api_key)
            model = genai.GenerativeModel('gemini-pro')
            
            # Generate response
            response = model.generate_content(
                f"You are a LinkedIn automation assistant. Help improve this message for professional networking: {message}"
            )
            
            ai_response = response.text
            flash('AI response generated successfully!', 'success')
            return render_template('ai_handler.html', 
                                 user=user,
                                 original_message=message,
                                 ai_response=ai_response)
            
        except Exception as e:
            flash(f'AI processing error: {str(e)}', 'error')
            return render_template('ai_handler.html', user=user)
    
    return render_template('ai_handler.html', user=user)
@application.route('/api/generate-message', methods=['POST'])
@login_required
@linkedin_setup_required
def api_generate_message():
    """Generate AI message for a specific contact"""
    try:
        user = get_current_user()
        data = request.json
        
        contact = data.get('contact', {})
        template = data.get('message_template', '')
        
        # Validate required contact info
        if not contact.get('Name'):
            return jsonify({'error': 'Contact name is required'}), 400
        
        # Configure Gemini AI
        import google.generativeai as genai
        genai.configure(api_key=user.gemini_api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # Build context for AI
        name = contact.get('Name', 'Professional')
        company = contact.get('Company', 'their company')
        role = contact.get('Role', 'their role')
        
        # Create enhanced prompt
        prompt = f"""Create a personalized LinkedIn outreach message based on:
        
Profile Information:
- Name: {name}
- Company: {company}  
- Role: {role}

Template/Guidelines: {template if template else 'Professional networking outreach'}

Requirements:
1. Address them by first name only
2. Reference their specific company and role
3. Be genuine and professional
4. Keep under 280 characters
5. Include a clear call to action
6. Avoid being overly salesy

Generate only the message text, no labels or formatting."""

        # Generate message with retries
        for attempt in range(3):
            try:
                response = model.generate_content(prompt)
                generated_message = response.text.strip()
                
                # Clean up the response
                generated_message = re.sub(r'^(Message:|Response:)\s*', '', generated_message, flags=re.IGNORECASE)
                generated_message = generated_message.strip('"\'[]')
                
                # Ensure length limit
                if len(generated_message) > 280:
                    generated_message = generated_message[:277] + "..."
                
                return jsonify({
                    'success': True,
                    'message': generated_message,
                    'contact': contact,
                    'character_count': len(generated_message)
                })
                
            except Exception as e:
                if "429" in str(e) or "rate limit" in str(e).lower():
                    wait_time = (attempt + 1) * 5
                    logger.warning(f"Rate limit hit, waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise e
        
        # Fallback message if AI fails
        fallback_message = f"Hi {name.split()[0]}, I came across your profile and was impressed by your work at {company}. I'd love to connect and learn more about your experience in {role}. Looking forward to connecting!"
        
        return jsonify({
            'success': True,
            'message': fallback_message[:280],
            'contact': contact,
            'character_count': len(fallback_message[:280]),
            'fallback': True
        })
        
    except Exception as e:
        logger.error(f"AI message generation error: {e}")
        return jsonify({'error': str(e)}), 500

# Add route for batch message preview
@application.route('/api/preview-campaign-messages', methods=['POST'])
@login_required
@linkedin_setup_required
def api_preview_campaign_messages():
    """Generate preview messages for multiple contacts in campaign"""
    try:
        user = get_current_user()
        data = request.json
        
        campaign_id = data.get('campaign_id')
        contacts = data.get('contacts', [])
        template = data.get('message_template', '')
        preview_count = min(int(data.get('preview_count', 5)), len(contacts))
        
        if not campaign_id or not contacts:
            return jsonify({'error': 'Campaign ID and contacts are required'}), 400
        
        # Configure Gemini AI
        import google.generativeai as genai
        genai.configure(api_key=user.gemini_api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        previews = []
        
        # Generate previews for first N contacts
        for i, contact in enumerate(contacts[:preview_count]):
            try:
                name = contact.get('Name', 'Professional')
                company = contact.get('Company', 'their company')
                role = contact.get('Role', 'their role')
                
                prompt = f"""Create a personalized LinkedIn outreach message for:
- Name: {name}
- Company: {company}  
- Role: {role}

Template: {template if template else 'Professional networking outreach'}

Keep under 280 characters, use first name only, be professional and genuine."""

                response = model.generate_content(prompt)
                generated_message = response.text.strip()
                generated_message = re.sub(r'^(Message:|Response:)\s*', '', generated_message, flags=re.IGNORECASE)
                generated_message = generated_message.strip('"\'[]')
                
                if len(generated_message) > 280:
                    generated_message = generated_message[:277] + "..."
                
                previews.append({
                    'index': i,
                    'contact': contact,
                    'message': generated_message,
                    'character_count': len(generated_message)
                })
                
                # Small delay to avoid rate limits
                time.sleep(0.5)
                
            except Exception as e:
                logger.warning(f"Failed to generate message for contact {i}: {e}")
                # Add fallback
                fallback = f"Hi {name.split()[0]}, I'd love to connect with you at {company}. Looking forward to networking!"
                previews.append({
                    'index': i,
                    'contact': contact,
                    'message': fallback,
                    'character_count': len(fallback),
                    'fallback': True
                })
        
        return jsonify({
            'success': True,
            'campaign_id': campaign_id,
            'previews': previews,
            'total_contacts': len(contacts),
            'preview_count': len(previews)
        })
        
    except Exception as e:
        logger.error(f"Campaign preview error: {e}")
        return jsonify({'error': str(e)}), 500
    
    
@application.route('/outreach', methods=['GET', 'POST'])
@login_required
@linkedin_setup_required  
def outreach():
    user = get_current_user()
    
    if request.method == 'POST':
        try:
            # Check if this is a preview request
            if request.form.get('action') == 'preview':
                return handle_campaign_preview(user)
            
            # Check if this is final campaign start
            if request.form.get('action') == 'start_campaign':
                return handle_campaign_start(user)
            
            # Default: File upload and initial setup
            return handle_file_upload(user)
            
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
            return render_template('outreach.html', user=user)
    
    return render_template('outreach.html', user=user)

def handle_file_upload(user):
    """Handle CSV file upload"""
    if 'csv_file' not in request.files:
        flash('No file selected!', 'error')
        return render_template('outreach.html', user=user)

    file = request.files['csv_file']
    if file.filename == '':
        flash('No file selected!', 'error')
        return render_template('outreach.html', user=user)

    if file and file.filename.lower().endswith('.csv'):
        filename = secure_filename(file.filename)
        filepath = os.path.join(application.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        # Process CSV file
        df = pd.read_csv(filepath)
        
        # Validate CSV structure
        required_columns = ['Name', 'Company', 'Role', 'LinkedIn_profile']
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            flash(f'CSV missing required columns: {missing_columns}', 'error')
            return render_template('outreach.html', user=user)

        # Get campaign settings
        max_contacts = int(request.form.get('max_contacts', 20))
        message_template = request.form.get('message_template', '')

        # Store campaign data
        campaign_data = {
            'file_path': filepath,
            'message_template': message_template,
            'max_contacts': max_contacts,
            'total_contacts': len(df),
            'contacts': df.to_dict('records')[:max_contacts],
            'campaign_id': str(uuid.uuid4()),
            'stage': 'uploaded'  # Track campaign stage
        }

        session['current_campaign'] = campaign_data
        flash(f'CSV uploaded successfully! {len(df)} contacts loaded.', 'success')
        
        return render_template('outreach.html',
                             user=user,
                             campaign_data=campaign_data,
                             show_preview_option=True)
    else:
        flash('Please upload a valid CSV file!', 'error')
        return render_template('outreach.html', user=user)

def handle_campaign_preview(user):
    """Handle campaign message preview generation"""
    campaign_data = session.get('current_campaign')
    if not campaign_data:
        flash('No campaign data found. Please upload a CSV first.', 'error')
        return render_template('outreach.html', user=user)
    
    try:
        # Generate preview messages
        preview_count = min(5, len(campaign_data['contacts']))
        
        # Make API call to generate previews
        preview_data = {
            'campaign_id': campaign_data['campaign_id'],
            'contacts': campaign_data['contacts'],
            'message_template': campaign_data['message_template'],
            'preview_count': preview_count
        }
        
        # Generate previews (call internal API)
        import google.generativeai as genai
        genai.configure(api_key=user.gemini_api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        previews = []
        for i, contact in enumerate(campaign_data['contacts'][:preview_count]):
            name = contact.get('Name', 'Professional')
            company = contact.get('Company', 'their company')
            role = contact.get('Role', 'their role')
            
            prompt = f"""Create a personalized LinkedIn message for {name} at {company} ({role}). 
Template: {campaign_data['message_template']}
Keep under 280 characters, professional tone."""

            try:
                response = model.generate_content(prompt)
                message = response.text.strip().strip('"\'[]')[:280]
                previews.append({
                    'contact': contact,
                    'message': message,
                    'character_count': len(message)
                })
            except Exception as e:
                fallback = f"Hi {name.split()[0]}, I'd love to connect and learn about your work at {company}!"
                previews.append({
                    'contact': contact,
                    'message': fallback,
                    'character_count': len(fallback),
                    'fallback': True
                })
        
        # Update campaign stage
        campaign_data['stage'] = 'previewed'
        campaign_data['message_previews'] = previews
        session['current_campaign'] = campaign_data
        
        flash(f'Generated {len(previews)} message previews!', 'success')
        return render_template('outreach.html',
                             user=user,
                             campaign_data=campaign_data,
                             show_start_option=True)
        
    except Exception as e:
        flash(f'Preview generation failed: {str(e)}', 'error')
        return render_template('outreach.html', user=user, campaign_data=campaign_data)

def handle_campaign_start(user):
    """Handle final campaign start after preview approval"""
    campaign_data = session.get('current_campaign')
    if not campaign_data or campaign_data.get('stage') != 'previewed':
        flash('Please preview messages before starting the campaign.', 'warning')
        return render_template('outreach.html', user=user)
    
    try:
        # Create task for client
        task = Task(
            user=user,
            task_type='outreach_campaign',
            params={
                'campaign_id': campaign_data['campaign_id'],
                'campaign_data': campaign_data,
                'user_config': {
                    'linkedin_email': user.linkedin_email,
                    'linkedin_password': user.linkedin_password,
                    'gemini_api_key': user.gemini_api_key
                }
            },
            status='queued'
        )
        task.save()
        
        # Clear session campaign data
        session.pop('current_campaign', None)
        
        flash('Campaign started successfully! Check the dashboard for progress.', 'success')
        return redirect(url_for('dashboard'))
        
    except Exception as e:
        flash(f'Failed to start campaign: {str(e)}', 'error')
        return render_template('outreach.html', user=user, campaign_data=campaign_data)


@application.route('/api/update-campaign-message', methods=['POST'])
@login_required
def api_update_campaign_message():
    """Update a specific message in the campaign preview"""
    try:
        data = request.json
        contact_index = data.get('contact_index')
        new_message = data.get('message', '').strip()
        
        campaign_data = session.get('current_campaign')
        if not campaign_data or 'message_previews' not in campaign_data:
            return jsonify({'error': 'No campaign preview found'}), 400
        
        if contact_index < 0 or contact_index >= len(campaign_data['message_previews']):
            return jsonify({'error': 'Invalid contact index'}), 400
        
        if len(new_message) > 280:
            return jsonify({'error': 'Message too long (max 280 characters)'}), 400
        
        # Update the message
        campaign_data['message_previews'][contact_index]['message'] = new_message
        campaign_data['message_previews'][contact_index]['character_count'] = len(new_message)
        campaign_data['message_previews'][contact_index]['edited'] = True
        
        # Save back to session
        session['current_campaign'] = campaign_data
        
        return jsonify({
            'success': True,
            'message': new_message,
            'character_count': len(new_message)
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@application.route('/start_collection', methods=['POST'])
@login_required
@linkedin_setup_required
def start_collection():
    user = get_current_user()
    try:
        sales_nav_url = request.form.get('sales_nav_url', '').strip()
        max_profiles = int(request.form.get('max_profiles', 25))

        if not sales_nav_url or "linkedin.com/sales/search/" not in sales_nav_url:
            flash('Please provide a valid LinkedIn Sales Navigator search URL.', 'error')
            return redirect(url_for('outreach'))

                # Always queue a collection task on the server side (do not require local client)
        collection_id = str(uuid.uuid4())
        collection_params = {
            'collection_id': collection_id,
            'sales_nav_url': sales_nav_url,
            'max_profiles': max_profiles
        }
        
        # Initialize cache for this collection (UI can poll this)
        collection_results_cache[collection_id] = {
            "status": "queued",
            "progress": 0,
            "total": max_profiles,
            "profiles": [],
            "start_time": datetime.now().isoformat(),
            "note": "Queued for processing; will begin when a worker/client picks the task."
        }

        # Create a DB task that worker/clients will pick up
        task = Task(
            user=user,
            task_type='collect_profiles',
            params={
                'collection_id': collection_id,
                'collection_params': collection_params,
                'user_config': {
                    'linkedin_email': user.linkedin_email,
                    'linkedin_password': user.linkedin_password,
                    'gemini_api_key': user.gemini_api_key
                }
            },
            status='queued'
        )
        task.save()

        flash(f'Profile collection has been queued for up to {max_profiles} profiles. You will be redirected to the results page.', 'success')
        session['current_collection_id'] = collection_id
        return redirect(url_for('campaign_builder', collection_id=collection_id))

    except Exception as e:
        flash(f'Error starting collection: {e}', 'error')
        return redirect(url_for('outreach'))

# NEW: Route to display collected profiles and build a campaign
@application.route('/campaign_builder/<collection_id>')
@login_required
def campaign_builder(collection_id):
    user = get_current_user()
    collection_data = collection_results_cache.get(collection_id)
    if not collection_data:
        flash('Collection data not found or expired.', 'error')
        return redirect(url_for('outreach'))

    return render_template('campaign_builder.html',
                           user=user,
                           collection_id=collection_id,
                           collection_data=collection_data)

# NEW: API endpoint to get collection status
@application.route('/collection_status/<collection_id>')
@login_required
def collection_status(collection_id):
    return jsonify(collection_results_cache.get(collection_id, {'status': 'not_found'}))


# NEW: Route to create a campaign from selected profiles
@application.route('/create_campaign_from_selection', methods=['POST'])
@login_required
def create_campaign_from_selection():
    try:
        collection_id = request.form.get('collection_id')
        selected_indices = request.form.getlist('profile_indices') # Checkbox values are indices

        if not collection_id or not selected_indices:
            flash('No profiles were selected to create the campaign.', 'warning')
            return redirect(url_for('campaign_builder', collection_id=collection_id))

        original_collection = collection_results_cache.get(collection_id)
        if not original_collection:
            flash('Collection data has expired. Please start a new collection.', 'error')
            return redirect(url_for('outreach'))

        # Build the contact list for the campaign
        selected_contacts = []
        all_profiles = original_collection.get('profiles', [])
        for index_str in selected_indices:
            try:
                index = int(index_str)
                if 0 <= index < len(all_profiles):
                    # Map scraped data to the required CSV format
                    profile = all_profiles[index]
                    contact = {
                        'Name': profile.get('name', 'N/A'),
                        'LinkedIn_profile': profile.get('profile_url', ''),
                        'Company': profile.get('company', 'N/A'),
                        'Role': profile.get('headline', 'N/A')
                    }
                    selected_contacts.append(contact)
            except ValueError:
                continue

        if not selected_contacts:
            flash('Could not process selected profiles. Please try again.', 'error')
            return redirect(url_for('campaign_builder', collection_id=collection_id))

        # Create and store the new campaign in the session, just like a CSV upload
        campaign_data = {
            'message_template': '', # User will define this on the outreach page
            'max_contacts': len(selected_contacts),
            'total_contacts': len(selected_contacts),
            'contacts': selected_contacts,
            'campaign_id': str(uuid.uuid4())
        }
        session['current_campaign'] = campaign_data
        
        flash(f'Successfully created a new campaign with {len(selected_contacts)} selected profiles!', 'success')
        return redirect(url_for('outreach'))

    except Exception as e:
        flash(f'Error creating campaign: {e}', 'error')
        return redirect(url_for('outreach'))

@application.route('/start_campaign', methods=['POST'])
@login_required
def start_campaign():
    user = get_current_user()
    campaign_id = request.json.get('campaign_id')

    campaign_data = session.get('current_campaign')
    if not campaign_data or campaign_data.get('campaign_id') != campaign_id:
            return jsonify({'error': 'Campaign not found in session'}), 404

    try:
        # --- This is the correct task queuing logic ---
        task = Task(
            user=user,
            task_type='outreach_campaign',
            params={
                'campaign_id': campaign_data['campaign_id'],
                'campaign_data': campaign_data,
                'user_config': {
                    'linkedin_email': user.linkedin_email,
                    'linkedin_password': user.linkedin_password,
                    'gemini_api_key': user.gemini_api_key
                }
            },
            status='queued'
        )
        task.save()
        
        logger.info(f"✅ Queued task {task.id} for user {user.email}")
        
        return jsonify({
            'success': True, 
            'message': 'Campaign has been queued for the client.',
            'task_id': str(task.id)
        })

    except Exception as e:
        logger.error(f"❌ Error queuing campaign task: {str(e)}")
        return jsonify({'error': str(e)}), 500

@application.route('/campaign_action', methods=['POST'])
@login_required
def campaign_action():
    user = get_current_user()
    data = request.json
    campaign_id = data.get('campaign_id')
    action = data.get('action')
    message = data.get('message')

    if not all([campaign_id, action]):
        return jsonify({'success': False, 'error': 'Missing parameters'}), 400
    
    payload = {
        'campaign_id': campaign_id,
        'action': action,
        'message': message,
        'contact_index': data.get('contact_index') # Pass index along
    }

    # --- THIS IS THE FIX ---
    # Create a persistent task instead of using the in-memory queue
    task = Task(
        user=user,
        task_type='campaign_action',
        params=payload, # Payload is already in the correct format
        status='queued'
    )
    task.save()
    
    return jsonify({'success': True, 'message': 'Campaign action queued.'})

@application.route('/stop_campaign', methods=['POST'])
@login_required
def stop_campaign():
    cid = request.json.get('campaign_id')
    if not cid:
        return jsonify({'error': 'campaign_id required'}), 400
    campaign_controls[cid]['stop'] = True
    automation_status['message'] = 'Stopping campaign...'
    return jsonify({'success': True})

@application.route('/contact_action', methods=['POST'])
@login_required
def contact_action():
    data = request.json
    cid = data.get('campaign_id')
    act = data.get('action')  # "send" | "skip"
    if cid not in campaign_controls or act not in ('send', 'skip'):
        return jsonify({'error': 'bad request'}), 400
    campaign_controls[cid]['action'] = act
    return jsonify({'success': True})

@application.route('/campaign_results/<campaign_id>')
@login_required
def get_campaign_results(campaign_id):
    # This now just returns the cached data, which is updated by the client
    return jsonify(campaign_results.get(campaign_id, {}))


@application.route('/campaign_status')
@login_required
def campaign_status():
    return jsonify(automation_status)

# In app.py, find and REPLACE the keyword_search function

@application.route('/keyword_search', methods=['GET', 'POST'])
@login_required
@linkedin_setup_required
def keyword_search():
    user = get_current_user()
    if request.method == 'POST':
        try:
            search_keywords = request.form.get('keywords', '').strip()
            # location is currently unused, but we'll keep it for future use
            location = request.form.get('location', '').strip()
            max_invites = int(request.form.get('max_invites', 10))

            if not search_keywords:
                flash('Please enter search keywords!', 'error')
                return render_template('keyword_search.html', user=user)

            # --- CONVERT TO TASK QUEUE ---
            task_params = {
                'keywords': search_keywords,
                'max_invites': max_invites
            }
            
            task = Task(
                user=user,
                task_type='keyword_search',
                params={
                    'search_id': str(uuid.uuid4()),
                    'user_config': {
                        'linkedin_email': user.linkedin_email,
                        'linkedin_password': user.linkedin_password,
                        'gemini_api_key': user.gemini_api_key
                    },
                    'search_params': {'keywords': search_keywords,
                                       'max_invites': max_invites,
                                       'search_type': 'search_and_connect'}
                },
                status='queued'
            )
            task.save()
            
            flash(f'Keyword search for "{search_keywords}" has been queued for the client. Monitoring task...', 'success')
            
            search_info={
                'search_id': str(task.id),
                'keywords': search_keywords,
            }
            return render_template('keyword_search.html', user=user, search_info=search_info)
            
        except Exception as e:
            flash(f'Search error: {str(e)}', 'error')
            return render_template('keyword_search.html', user=user)
    
    # GET request - show form
    return render_template('keyword_search.html', user=user)

@application.route('/search_results/<search_id>')
@login_required
def get_search_results(search_id):
    results = search_results_cache.get(search_id, {})
    return jsonify(results)

@application.route('/preview_message', methods=['POST'])
@login_required
def preview_message():
    """Preview message before sending in campaign - ENHANCED"""
    try:
        data = request.json
        campaign_id = data.get('campaign_id')
        
        # Get current campaign status from client
        user = get_current_user()
        
        # The client now reports progress directly, so we check our cache
        campaign_status = campaign_results.get(campaign_id, {})
                
        if campaign_status.get('awaiting_confirmation'):
            current_contact_data = campaign_status.get('current_contact_preview', {})
            
            return jsonify({
                'success': True,
                'awaiting_confirmation': True,
                'contact': current_contact_data.get('contact', {}),
                'generated_message': current_contact_data.get('message', ''),
                'contact_index': current_contact_data.get('contact_index', 0)
            })
                    
        return jsonify({'success': False, 'error': 'No preview available'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@application.route('/api/get-tasks', methods=['POST'])
def api_get_tasks():
    """
    Client polling endpoint - serves only long-running tasks from the database.
    """
    auth = request.headers.get('Authorization', '')
    api_key = auth.replace('Bearer ', '').strip() if auth.startswith('Bearer ') else None
    
    if not api_key:
        return jsonify({'error': 'Missing API key'}), 401

    user = User.objects(gemini_api_key=api_key).first()
    if not user:
        return jsonify({'error': 'Invalid API key'}), 403

    tasks = []
    
    # Get new, long-running tasks from the database for this user ONLY
    db_tasks = Task.objects(user=user, status='queued').order_by('+created_at')
    for task in db_tasks:
        tasks.append({
            'id': str(task.id),
            'type': task.task_type,
            'params': task.params or {}
        })
        task.status = 'processing'
        task.save()
    
    if not tasks:
        return ('', 204)  # No Content - No tasks available
    
    return jsonify({'tasks': tasks})

# Replace existing /api/report-task handler with this:
@application.route('/api/report-task', methods=['POST'])
def api_report_task():
    """Client reports generated message/progress back to dashboard.
    Supports interim updates (which update results but don't stop the task)
    and final task results (which persist and close the task).
    """
    try:
        auth = request.headers.get('Authorization', '')
        api_key = auth.replace('Bearer ', '').strip() if auth.startswith('Bearer ') else None
        
        if not api_key:
            return jsonify({'error': 'Missing API key'}), 401

        user = User.objects(gemini_api_key=api_key).first()
        if not user:
            return jsonify({'error': 'Invalid API key'}), 403

        data = request.json or {}
        
        # --- NEW: Check for interim flag ---
        is_interim = data.get('interim_update', False)

        task_id = data.get('task_id') or data.get('taskId')
        task_type = data.get('type') or data.get('task_type')
        success_flag = data.get('success', None)
        payload = data.get('payload') or data.get('result') or data.get('results') or {}
        
        if not task_id:
            logger.warning(f"Report received without task_id: {data.get('type')}")
            # Fallback for old preview-mode (non-task based)
            if 'contact' in data:
                automation_status['awaiting'] = True
                automation_status['message'] = data.get('message', '')
                automation_status['current'] = {
                    'task_id': task_id,
                    'contact': data.get('contact', {}),
                    'message': data.get('message', ''),
                    'timestamp': datetime.utcnow().isoformat()
                }
                return jsonify({'success': True, 'preview': True}), 200
            return jsonify({'error': 'Missing task_id'}), 400

        # Try to update the Task in the DB
        try:
            task_obj = Task.objects.get(id=task_id)
        except Exception:
            task_obj = None

        result_obj = payload if isinstance(payload, dict) else {}

        if task_obj:
            # --- MODIFIED LOGIC ---
            
            # Always update the result object
            try:
                task_obj.result = result_obj
            except Exception:
                task_obj.result = json.loads(json.dumps(result_obj, default=str))

            # ONLY update status if this is NOT an interim report
            if not is_interim:
                task_obj.status = 'completed' if (success_flag or result_obj.get('success')) else 'failed'
                task_obj.error = data.get('error')
                task_obj.completed_at = datetime.utcnow()
            
            task_obj.save()
            
            if is_interim:
                logger.info(f"📈 Updated interim stats for task {task_id} (type={task_obj.task_type})")
            else:
                logger.info(f"✅ Persisted FINAL task result for task {task_id} (type={task_obj.task_type})")
            
            return jsonify({'success': True, 'stored': True}), 200
            
        else:
            # Task not found in DB (should not happen for inbox tasks)
            # Store in cache as a fallback
            if (task_type == 'keyword_search') or ('invites_sent' in result_obj):
                search_results_cache[task_id] = {'results': result_obj}
            elif (task_type == 'process_inbox') or ('auto_replied' in result_obj):
                inbox_results[task_id] = result_obj
            
            logger.warning(f"⚠️ Task {task_id} not found in DB. Stored result in memory cache.")
            return jsonify({'success': True, 'stored_in_cache': True}), 200

    except Exception as e:
        logger.error(f"api_report_task error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

def get_user_from_api_key():
    """Authenticates a user from the 'Authorization: Bearer <key>' header."""
    auth = request.headers.get('Authorization', '')
    api_key = auth.replace('Bearer ', '').strip() if auth.startswith('Bearer ') else None
    
    if not api_key:
        return None
    
    user = User.objects(gemini_api_key=api_key).first()
    return user

@application.route('/api/google/free-slots', methods=['GET'])
def api_google_free_slots():
    """
    Finds and returns free calendar slots for the authenticated user.
    """
    user = get_user_from_api_key()
    if not user:
        return jsonify({'error': 'Invalid API key'}), 403
        
    if not user.google_refresh_token:
        return jsonify({'error': 'Google Account not connected for this user.'}), 400

    try:
        duration = int(request.args.get('duration_minutes', 30))
        days = int(request.args.get('days_ahead', 7))
        
        # 
        slots = google_services.find_free_slots(
            user=user, 
            duration_minutes=duration, 
            days_ahead=days
        )
        
        # Convert datetime objects to ISO strings for JSON
        slot_strings = [slot.isoformat() for slot in slots]
        
        return jsonify({'success': True, 'slots': slot_strings})
        
    except Exception as e:
        logger.error(f"Error finding free slots: {e}")
        return jsonify({'error': str(e)}), 500
    
@application.route('/api/google/book-meeting', methods=['POST'])
def api_google_book_meeting():
    """
    Books a new event on the user's calendar.
    """
    user = get_user_from_api_key()
    if not user:
        return jsonify({'error': 'Invalid API key'}), 403

    data = request.json
    try:
        summary = data.get('summary')
        start_time_str = data.get('start_time')
        end_time_str = data.get('end_time')
        attendee_email = data.get('attendee_email')

        if not all([summary, start_time_str, end_time_str, attendee_email]):
            return jsonify({'error': 'Missing required fields (summary, start_time, end_time, attendee_email)'}), 400

        # Convert ISO strings back to datetime objects
        start_dt = datetime.fromisoformat(start_time_str)
        end_dt = datetime.fromisoformat(end_time_str)
        
        # 
        event = google_services.create_event(
            user=user,
            summary=summary,
            start_dt=start_dt,
            end_dt=end_dt,
            attendee_email=attendee_email
        )
        
        meet_link = event.get('hangoutLink')
        return jsonify({'success': True, 'event_id': event.get('id'), 'meet_link': meet_link})
        
    except Exception as e:
        logger.error(f"Error creating event: {e}")
        return jsonify({'error': str(e)}), 500

@application.route('/api/google/send-email', methods=['POST'])
def api_google_send_email():
    """
    Sends an email from the user's Gmail account.
    """
    user = get_user_from_api_key()
    if not user:
        return jsonify({'error': 'Invalid API key'}), 403

    data = request.json
    try:
        to_email = data.get('to_email')
        subject = data.get('subject')
        body = data.get('body')

        if not all([to_email, subject, body]):
            return jsonify({'error': 'Missing required fields (to_email, subject, body)'}), 400
        
        # [cite: 13]
        result = google_services.send_email(
            user=user,
            to_email=to_email,
            subject=subject,
            body=body
        )
        
        return jsonify({'success': True, 'message_id': result.get('id')})
        
    except Exception as e:
        logger.error(f"Error sending email: {e}")
        return jsonify({'error': str(e)}), 500
# Add to app.py
@application.route('/api/task-result', methods=['POST'])
def api_task_result():
    """Receive task results from clients"""
    try:
        data = request.json
        task_id = data.get('task_id')
        success = data.get('success', False)
        result = data.get('result', {})
        error = data.get('error')
        
        if not task_id:
            return jsonify({'error': 'Missing task_id'}), 400
            
        # Update the task in the database
        task = Task.objects.get(id=task_id)
        task.status = 'completed' if success else 'failed'
        task.result = result
        task.error = error
        task.completed_at = datetime.utcnow()
        task.save()
        
        # If it's a campaign, update campaign results
        if task.task_type == 'outreach_campaign':
            campaign_id = task.params.get('campaign_data', {}).get('campaign_id')
            if campaign_id:
                campaign_results[campaign_id] = result
        
        return jsonify({'success': True})
        
    except Task.DoesNotExist:
        # Check if it's a search result
        if 'search_id' in data:
             search_results_cache[data['search_id']] = result
             return jsonify({'success': True})
        return jsonify({'error': 'Task not found'}), 404
    except Exception as e:
        logger.error(f"Error processing task result: {e}")
        return jsonify({'error': str(e)}), 500

@application.route('/confirm_message_action', methods=['POST'])
@login_required
def confirm_message_action():
    """Handle user decision on message (send/skip/edit)"""
    try:
        data = request.json
        action = data.get('action')  # 'send', 'skip', 'edit'
        campaign_id = data.get('campaign_id')
        contact_index = data.get('contact_index')
        message = data.get('message', '')
        
        if action not in ['send', 'skip', 'edit']:
            return jsonify({'error': 'Invalid action'}), 400
            
        # Store user decision for the campaign worker
        user = get_current_user()
        
        # Send decision to local client
        payload = {
            'campaign_id': campaign_id,
            'action': action,
            'contact_index': contact_index,
            'message': message
        }
        
        result = client_manager.send_campaign_action(str(user.id), payload)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@application.route('/api/dashboard-status')
@login_required
def api_dashboard_status():
    """Get dashboard status including client connectivity"""
    user = get_current_user()
    client_id = str(user.id)
    
    client_status = client_manager.get_client_status(client_id)
    
    # Get task counts
    queued_tasks = Task.objects(user=user, status='queued').count()
    processing_tasks = Task.objects(user=user, status='processing').count()
    
    user_has_setup = False
    try:
        user_has_setup = user.has_linkedin_setup()
    except Exception:
        # Fallback: check necessary fields
        user_has_setup = all([
            getattr(user, 'linkedin_email', None),
            getattr(user, 'linkedin_password', None),
            getattr(user, 'gemini_api_key', None)
        ])

    features_available = {
        'ai_inbox': user_has_setup,
        'outreach': user_has_setup,
        'keyword_search': user_has_setup
    }

    return jsonify({
        'client': client_status,
        'tasks': {
            'queued': queued_tasks,
            'processing': processing_tasks
        },
        'features_available': features_available
    })

@application.route('/api/inbox_results', methods=['POST'])
def api_inbox_results():
    """
    Clients report task results here.
    """
    auth = request.headers.get('Authorization', '')
    api_key = None
    if auth.startswith('Bearer '):
        api_key = auth.replace('Bearer ', '').strip()
    
    if not api_key:
        return jsonify({'error': 'Missing API key'}), 401

    user = User.objects(gemini_api_key=api_key).first()
    if not user:
        return jsonify({'error': 'Invalid API key'}), 403

    payload = request.get_json() or {}
    task_id = payload.get('process_id') or payload.get('task_id')
    results = payload.get('results')

    if not task_id:
        return jsonify({'error': 'Missing task_id or process_id'}), 400

    # Store results in memory cache
    inbox_results[task_id] = results
    logger.info(f"✅ Stored inbox results for task {task_id}")


    return jsonify({'success': True}), 200


@application.route('/ai_inbox', methods=['GET', 'POST'])
@login_required
@linkedin_setup_required
def ai_inbox():
    user = get_current_user()
    
    if request.method == 'POST':
        try:
            process_id = str(uuid.uuid4())
            task=Task(
                user=user,
                task_type='process_inbox',
                params={'process_id': process_id},
                status='queued'
            )
            task.save()
            logger.info(f"✅ Queued 'process_inbox' task {task.id} for user {user.email}")
            
            # FIX: Return a JSON response instead of redirecting
            return jsonify({
                'success': True, 
                'message': 'Task queued successfully!', 
                'task_id': process_id  # Use the same ID for polling
            })
            
        except Exception as e:
            logger.error(f"Inbox processing error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500
    
    # GET request - show the page
    # You can add logic here to find the latest running task for this user
    # and pass its ID to the template as `current_inbox_process_id`
    return render_template('ai_inbox.html', user=user)
inbox_preview_states = {}

# In app.py - Replace the @app.route('/api/inbox_preview/<process_id>') GET endpoint
@application.route('/api/inbox_preview/<session_id>', methods=['GET'])
@login_required
def get_inbox_preview_status(session_id):
    """
    Frontend polls this endpoint to check if there is a preview waiting for user action.
    """
    preview_state = inbox_preview_states.get(session_id)
    
    if preview_state and preview_state.get('awaiting_confirmation'):
        return jsonify(preview_state)
    else:
        # No preview is waiting for this session
        return jsonify({'awaiting_confirmation': False})
    
@application.route('/api/inbox_preview/process/<process_id>', methods=['GET'])
def get_inbox_preview_by_process(process_id):
    """Get current inbox preview state for a process (fallback route)"""
    # This searches through all sessions to find matching process_id
    for session_id, preview_data in inbox_preview_states.items():
        if preview_data.get('process_id') == process_id:
            return get_inbox_preview_by_session(session_id)
    
    return jsonify({'awaiting_confirmation': False})

@application.route('/api/inbox_action', methods=['POST'])
@login_required
def inbox_action():
    """
    Receives the user's decision (send/edit/skip) from the frontend
    and queues it as a persistent task for the client.
    """
    user = get_current_user()
    data = request.json
    
    session_id = data.get('session_id')
    action = data.get('action')
    message = data.get('message')

    if not session_id or not action:
        return jsonify({'success': False, 'error': 'Missing session_id or action'}), 400

    logger.info(f"User '{user.email}' took action '{action}' for inbox session '{session_id}'")

    # --- THIS IS THE FIX ---
    # Create a persistent task instead of using the in-memory queue
    task = Task(
        user=user,
        task_type='inbox_action',
        params={
            'session_id': session_id,
            'action': action,
            'message': message,
        },
        status='queued'
    )
    task.save()
    
    if session_id in inbox_preview_states:
        inbox_preview_states[session_id]['awaiting_confirmation'] = False
        inbox_preview_states[session_id]['action_taken'] = action
    
    return jsonify({
        'success': True,
        'message': f"Action '{action}' was successfully queued for the client."
    })

inbox_preview_states = {}

@application.route('/api/inbox_preview', methods=['POST'])
def api_inbox_preview():
    """
    Receives an inbox preview from a client and stores it server-side.
    """
    try:
        # Authenticate the client via API key
        auth_header = request.headers.get('Authorization', '')
        api_key = auth_header.replace('Bearer ', '').strip() if auth_header.startswith('Bearer ') else None
        user = User.objects(gemini_api_key=api_key).first()
        if not user:
            return jsonify({'error': 'Invalid API key'}), 403

        data = request.json or {}
        session_id = data.get('session_id')
        preview_data = data.get('preview')

        if not session_id or not preview_data:
            return jsonify({'error': 'Missing session_id or preview data'}), 400

        # Store the state for the frontend to poll
        inbox_preview_states[session_id] = {
            'awaiting_confirmation': True,
            'preview': preview_data,
            'user_id': str(user.id),
            'timestamp': datetime.now().isoformat()
        }
        
        contact_name = preview_data.get('contact', {}).get('name', 'Unknown')
        logger.info(f"✅ Stored inbox preview for session {session_id} (Contact: {contact_name})")
        
        return jsonify({'success': True}), 200

    except Exception as e:
        logger.error(f"Error storing inbox preview: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

@application.route('/inbox_results/<process_id>', methods=['GET'])
def get_inbox_results_by_process(process_id):
    """Get inbox processing results"""
    try:
        # Return mock results for testing
        return jsonify({
            "success": True,
            "process_id": process_id,
            "auto_replied": 1,
            "skipped": 0, 
            "errors": 0,
            "summary": {
                "processed_count": 1,
                "auto_reply_count": 1,
                "skipped_count": 0
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@application.route('/inbox_results/<process_id>')
@login_required
def get_inbox_results(process_id):
    """Get inbox results for a specific process - FIXED VERSION"""
    # Check both inbox_results and inbox_preview_states
    results = inbox_results.get(process_id, {})
    
    # Also check if there's a preview state for this process
    if process_id in inbox_preview_states:
        preview_data = inbox_preview_states[process_id]
        results.update({
            'awaiting_confirmation': preview_data.get('awaiting_confirmation', False),
            'current_reply_preview': preview_data.get('preview', {}),
            'status': 'awaiting_action'
        })
    
    return jsonify(results)

# Update the client ping endpoint to include inbox actions
@application.route('/api/client-ping', methods=['POST'])
def api_client_ping():
    """Client heartbeat endpoint."""
    try:
        auth = request.headers.get('Authorization', '')
        api_key = auth.replace('Bearer ', '').strip() if auth.startswith('Bearer ') else None
        
        if not api_key:
            return jsonify({'error': 'Missing API key'}), 401

        user = User.objects(gemini_api_key=api_key).first()
        if not user:
            return jsonify({'error': 'Invalid API key'}), 403

        user_id = str(user.id)
        
        data = request.json or {}
        client_id = data.get('client_id')
        client_info = data.get('client_info', {})
        
        # ... (keep all the inbox_preview_states logic here) ...
        active_inbox_sessions = data.get('active_inbox_sessions', [])
        for session_data in active_inbox_sessions:
             # ... (all this logic is fine) ...
            session_id = session_data.get('session_id')
            conversation = session_data.get('conversation')
            if session_id and conversation:
                process_id = conversation.get('process_id') or session_id
                inbox_preview_states[session_id] = {
                    'session_id': session_id,
                    'process_id': process_id,
                    'awaiting_confirmation': True,
                    'preview': conversation,
                    'timestamp': datetime.now().isoformat()
                }
        
        if not client_id:
            return jsonify({'error': 'Missing client_id in request body'}), 400
        
        # Update client status for monitoring
        client_manager.update_client_heartbeat(user_id, client_id, client_info)
        
        return jsonify({
            'success': True, 
            'server_time': datetime.utcnow().isoformat(),
            'actions': []  # Always send an empty list now
        })
    
    except Exception as e:
        logger.error(f"Client ping error: {e}")
        return jsonify({'error': str(e)}), 500
    
@application.route('/api/campaign-progress/<campaign_id>')
@login_required
def get_campaign_progress(campaign_id):
    """Get campaign progress for frontend display"""
    progress = campaign_results.get(campaign_id, {})
    return jsonify(progress)
    
@application.route('/api/create-task', methods=['POST'])
def api_create_task():
    """
    Dashboard/admin can create tasks for a user.
    Expects: { "user_id": "...", "type": "process_inbox", "params": {...} }
    """
    data = request.get_json() or {}
    user_id = data.get("user_id")
    ttype = data.get("type")
    params = data.get("params", {})

    if not user_id or not ttype:
        return jsonify({'error': 'user_id and type are required'}), 400

    user = User.objects(id=user_id).first()
    if not user:
        return jsonify({'error': 'User not found'}), 404

    task = Task(
        user=user,
        task_id=str(uuid.uuid4()),
        type=ttype,
        params=params
    ).save()

    return jsonify({'success': True, 'task_id': task.task_id}), 201



@application.route('/api/campaign_progress', methods=['POST'])
def receive_campaign_progress():
    """Receive campaign progress from local client"""
    try:
        # Get API key from headers
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Missing or invalid Authorization header'}), 401
            
        api_key = auth_header.replace('Bearer ', '').strip()
        user = User.objects(gemini_api_key=api_key).first()
        
        if not user:
            return jsonify({'error': 'Invalid API key'}), 403

        data = request.json
        campaign_id = data.get('campaign_id')
        progress = data.get('progress', {})
        is_final = data.get('final', False)
        
        # Store progress in campaign_results
        campaign_results[campaign_id] = progress
        
        # Log the received progress for debugging
        logger.info(f"Received progress for campaign {campaign_id}: {progress}")
        
        if is_final:
            logger.info(f"✅ Campaign {campaign_id} completed")
        
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"❌ Error receiving campaign progress: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@application.route('/api/search_results', methods=['POST'])
def receive_search_results():
    """Receive search results from local client"""
    try:
        data = request.json
        search_id = data.get('search_id')
        results = data.get('results', {})
        
        # Store results in search_results_cache
        search_results_cache[search_id] = {
            'type': 'client_search',
            'results': results,
            'timestamp': datetime.now().isoformat()
        }
        
        logger.info(f"✅ Received search results for {search_id}")
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"❌ Error receiving search results: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@application.route('/authorize-google')
@login_required
def authorize_google():
    """
    Redirects the user to Google's OAuth 2.0 consent screen.
    """
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    # Create the full, absolute URL for our callback
    # _external=True is critical
    redirect_uri = url_for('oauth2callback', _external=True)
    
    flow = google_services.create_google_auth_flow(redirect_uri)
    
    # Generate the authorization URL and store the state for CSRF protection
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
    )
    
    # Store the state in the user's session
    session['google_oauth_state'] = state
    
    print(f"Redirecting user to: {authorization_url} (state={state})")
    return redirect(authorization_url)


@application.route('/oauth2callback')
@login_required
def oauth2callback():
    """
    Handles the callback from Google after user consent.
    """
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    # Verify the state to protect against CSRF
    state = session.pop('google_oauth_state', None)
    if state is None or state != request.args.get('state'):
        flash('Invalid state. Authentication request denied.', 'error')
        return redirect(url_for('settings'))

    # Recreate the flow with the same redirect_uri
    redirect_uri = url_for('oauth2callback', _external=True)
    flow = google_services.create_google_auth_flow(redirect_uri)

    try:
        # Exchange the authorization code for credentials
        # We pass the full request URL
        flow.fetch_token(authorization_response=request.url)
        
        credentials = flow.credentials
        
        # Save the all-important REFRESH token and scopes to the user
        user.google_refresh_token = credentials.refresh_token
        user.google_scopes = credentials.scopes
        user.save()
        
        flash('Google Account connected successfully!', 'success')
        
    except Exception as e:
        flash(f'Failed to connect Google Account: {str(e)}', 'error')
        print(f"Error in oauth2callback: {e}")

    return redirect(url_for('settings'))

@application.route('/calendar')
@login_required
def calendar_view():
    """Render the Calendar/Scheduler page"""
    user = get_current_user()
    # Check if Google is connected
    google_connected = bool(user.google_refresh_token)
    return render_template('calendar.html', user=user, google_connected=google_connected)

@application.route('/api/google/upcoming-events', methods=['GET'])
def api_get_upcoming_events():
    """Fetch upcoming 10 events from Google Calendar"""
    user = get_user_from_api_key() # OR use session based auth if calling from frontend JS
    if not user and 'user_id' in session:
         user = User.objects.get(id=session['user_id'])
         
    if not user or not user.google_refresh_token:
        return jsonify({'error': 'Google not connected'}), 401

    try:
        # You will need to add 'list_events' to your google_services.py
        # passing max_results=10
        events = google_services.list_upcoming_events(user, max_results=10)
        return jsonify({'success': True, 'events': events})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@application.route('/logout')
@login_required
def logout():
    user_name = session.get('user_name', '')
    session.clear()
    
    flash(f'Goodbye, {user_name}!', 'info')
    return redirect(url_for('login'))

@application.errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404

@application.errorhandler(500)
def internal_error(error):
    return render_template('500.html'), 500

if __name__ == '__main__':
    print(f"Starting Flask app...")
    print(f"Project directory: {basedir}")
    print(f"Upload folder: {application.config['UPLOAD_FOLDER']}")

    application.run(debug=True, host='0.0.0.0', port=5000)