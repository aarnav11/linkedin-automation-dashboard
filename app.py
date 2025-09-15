from asyncio import tasks
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import os
import pandas as pd
from werkzeug.utils import secure_filename
from models import User, Task
from linkedin_automation import LinkedInAutomation
from datetime import datetime
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

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-here-change-this-in-production')

# MongoDB Atlas connection
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb+srv://Arnav-admin:YourActualPassword@linkedin-auto-users.qt8v57k.mongodb.net/?retryWrites=true&w=majority&appName=Linkedin-auto-users")
connect(host=MONGODB_URI)

# File upload configuration
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

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
        self.clients = {}  # client_id -> client info
        self.client_tasks = defaultdict(list)
    
    def register_client(self, client_id, client_info):
        self.clients[client_id] = client_info
    
    def is_client_available(self, client_id):
        return client_id in self.clients
    
    def get_client_url(self, client_id):
        if client_id in self.clients:
            return self.clients[client_id].get('client_url')
        return None
    
    def send_task_to_client(self, client_id, task_data):
        if client_id in self.clients:
            # Store task for when client polls
            self.client_tasks[client_id].append(task_data)
            return {'success': True}
        return {'success': False, 'error': 'Client not registered'}
    
    def get_client_tasks(self, client_id):
        if client_id in self.client_tasks:
            tasks = self.client_tasks[client_id]
            self.client_tasks[client_id] = []  # Clear tasks after retrieving
            return tasks
        return []

# Initialize the client manager
client_manager = ClientManager()

@app.route('/client_setup')
@login_required
def client_setup():
    """Show client setup instructions"""
    user = get_current_user()
    client_available = client_manager.is_client_available(str(user.id))
    
    return render_template('client_setup.html', 
                         user=user, 
                         client_available=client_available,
                         client_url=client_manager.get_client_url(str(user.id)))

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
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

@app.route('/login', methods=['GET', 'POST'])
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

@app.route('/dashboard')
@login_required
def dashboard():
    user = get_current_user()
    
    # Get user statistics (placeholder data)
    stats = {
        'active_campaigns': 0,
        'messages_sent': 0,
        'connections_made': 0,
        'total_contacts': 0
    }
    
    return render_template('dashboard.html', 
                         user=user,
                         stats=stats)

@app.route('/profile')
@login_required
def profile():
    user = get_current_user()
    return render_template('profile.html', user=user)

@app.route('/settings', methods=['GET', 'POST'])
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

@app.route('/ai_handler', methods=['GET', 'POST'])
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

@app.route('/outreach', methods=['GET', 'POST'])
@login_required
@linkedin_setup_required
def outreach():
    user = get_current_user()
    
    if request.method == 'POST':
        try:
            if 'csv_file' not in request.files:
                flash('No file selected!', 'error')
                return render_template('outreach.html', user=user)
            
            file = request.files['csv_file']
            if file.filename == '':
                flash('No file selected!', 'error')
                return render_template('outreach.html', user=user)
            
            if file and file.filename.lower().endswith('.csv'):
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                
                # Process CSV file
                df = pd.read_csv(filepath)
                
                # Validate CSV structure for test.csv format
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
                    'contacts': df.to_dict('records'),
                    'campaign_id': str(uuid.uuid4())
                }
                
                session['current_campaign'] = campaign_data
                flash(f'CSV uploaded successfully! {len(df)} contacts loaded.', 'success')
                return render_template('outreach.html', 
                                     user=user,
                                     campaign_data=campaign_data)
            else:
                flash('Please upload a valid CSV file!', 'error')
                return render_template('outreach.html', user=user)
                
        except Exception as e:
            flash(f'File processing error: {str(e)}', 'error')
            return render_template('outreach.html', user=user)
    
    return render_template('outreach.html', user=user)
@app.route('/start_collection', methods=['POST'])
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

        if not client_manager.is_client_available(str(user.id)):
            flash('Local client is not running. Please start it to collect profiles.', 'error')
            return redirect(url_for('client_setup'))

        collection_id = str(uuid.uuid4())
        collection_params = {
            'collection_id': collection_id,
            'sales_nav_url': sales_nav_url,
            'max_profiles': max_profiles
        }
        
        # Initialize cache for this collection
        collection_results_cache[collection_id] = {
            "status": "starting",
            "progress": 0,
            "total": max_profiles,
            "profiles": [],
            "start_time": datetime.now().isoformat()
        }

        result = client_manager.send_collection_request(str(user.id), collection_params)

        if result.get('success'):
            flash(f'Profile collection started for up to {max_profiles} profiles. You will be redirected to the results page.', 'success')
            session['current_collection_id'] = collection_id
            return redirect(url_for('campaign_builder', collection_id=collection_id))
        else:
            flash(f"Failed to start collection: {result.get('error', 'Unknown error')}", 'error')
            collection_results_cache.pop(collection_id, None) # Clean up
            return redirect(url_for('outreach'))

    except Exception as e:
        flash(f'Error starting collection: {e}', 'error')
        return redirect(url_for('outreach'))

# NEW: Route to display collected profiles and build a campaign
@app.route('/campaign_builder/<collection_id>')
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
@app.route('/collection_status/<collection_id>')
@login_required
def collection_status(collection_id):
    return jsonify(collection_results_cache.get(collection_id, {'status': 'not_found'}))


# NEW: Route to create a campaign from selected profiles
@app.route('/create_campaign_from_selection', methods=['POST'])
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

@app.route('/start_campaign', methods=['POST'])
@login_required
def start_campaign():
    user = get_current_user()
    campaign_id = request.json.get('campaign_id')

    campaign_data = session.get('current_campaign')
    if not campaign_data or campaign_data.get('campaign_id') != campaign_id:
            return jsonify({'error': 'Campaign not found in session'}), 404

    try:
        # Create the task and save it to the database
        task = Task(
            user=user,
            task_type='start_campaign',
            params={
                'campaign_id': campaign_data['campaign_id'],
                'user_config': {
                    'linkedin_email': user.linkedin_email,
                    'linkedin_password': user.linkedin_password,
                    'gemini_api_key': user.gemini_api_key},
                'campaign_data': campaign_data
            },
            status='queued'
        )
        task.save()
        
        logger.info(f"✅ Queued task {task.id} for user {user.email}")
        
        # Give immediate feedback to the user on the dashboard
        return jsonify({
            'success': True, 
            'message': 'Campaign has been queued for the client.',
            'task_id': str(task.id)
        })

    except Exception as e:
        logger.error(f"❌ Error queuing campaign task: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/campaign_action', methods=['POST'])
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
        'message': message
    }

    result = client_manager.send_campaign_action(str(user.id), payload)
    return jsonify(result)

@app.route('/stop_campaign', methods=['POST'])
@login_required
def stop_campaign():
    cid = request.json.get('campaign_id')
    if not cid:
        return jsonify({'error': 'campaign_id required'}), 400
    campaign_controls[cid]['stop'] = True
    automation_status['message'] = 'Stopping campaign...'
    return jsonify({'success': True})

@app.route('/contact_action', methods=['POST'])
@login_required
def contact_action():
    data = request.json
    cid = data.get('campaign_id')
    act = data.get('action')  # "send" | "skip"
    if cid not in campaign_controls or act not in ('send', 'skip'):
        return jsonify({'error': 'bad request'}), 400
    campaign_controls[cid]['action'] = act
    return jsonify({'success': True})

@app.route('/campaign_results/<campaign_id>')
@login_required
def get_campaign_results(campaign_id):
    user = get_current_user()
    client_url = client_manager.get_client_url(str(user.id))
    try:
        response = requests.get(f"{client_url}/campaign_status/{campaign_id}", timeout=5)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            # Fallback to local cache if client is down
            return jsonify(campaign_results.get(campaign_id, {}))
    except Exception:
        return jsonify(campaign_results.get(campaign_id, {}))

@app.route('/campaign_status')
@login_required
def campaign_status():
    return jsonify(automation_status)

# In app.py, find and REPLACE the keyword_search function

@app.route('/keyword_search', methods=['GET', 'POST'])
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
            
            flash(f'Keyword search for "{search_keywords}" has been queued for the client.', 'success')
            return redirect(url_for('keyword_search'))
            
        except Exception as e:
            flash(f'Search error: {str(e)}', 'error')
            return render_template('keyword_search.html', user=user)
    
    # GET request - show form
    return render_template('keyword_search.html', user=user)

@app.route('/search_results/<search_id>')
@login_required
def get_search_results(search_id):
    results = search_results_cache.get(search_id, {})
    return jsonify(results)

@app.route('/preview_message', methods=['POST'])
@login_required
def preview_message():
    """Preview message before sending in campaign - ENHANCED"""
    try:
        data = request.json
        campaign_id = data.get('campaign_id')
        
        # Get current campaign status from client
        user = get_current_user()
        client_url = client_manager.get_client_url(str(user.id))
        
        try:
            response = requests.get(f"{client_url}/campaign_status/{campaign_id}", timeout=5)
            if response.status_code == 200:
                campaign_status = response.json()
                
                if campaign_status.get('awaiting_confirmation'):
                    current_contact_data = campaign_status.get('current_contact_preview', {})
                    
                    return jsonify({
                        'success': True,
                        'awaiting_confirmation': True,
                        'contact': current_contact_data.get('contact', {}),
                        'generated_message': current_contact_data.get('message', ''),
                        'contact_index': current_contact_data.get('contact_index', 0)
                    })
                    
        except Exception as e:
            logger.error(f"Error getting campaign status: {e}")
            
        return jsonify({'success': False, 'error': 'No preview available'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@app.route('/api/get-tasks', methods=['POST'])
def api_get_tasks():
    """
    Client polling endpoint - enhanced for client manager
    """
    auth = request.headers.get('Authorization', '')
    api_key = None
    if auth.startswith('Bearer '):
        api_key = auth.replace('Bearer ', '').strip()
    else:
        api_key = (request.json or {}).get('api_key')

    if not api_key:
        return jsonify({'error': 'Missing API key'}), 401

    # Validate API key and get user
    user = User.objects(gemini_api_key=api_key).first()
    if not user:
        return jsonify({'error': 'Invalid API key'}), 403

    # Get client ID from request
    client_id = (request.json or {}).get('client_id')
    if not client_id:
        return jsonify({'error': 'Missing client ID'}), 400
        
    # Register/update client info
    client_info = {
        'last_seen': datetime.utcnow(),
        'client_url': request.json.get('client_url'),
        'user_agent': request.headers.get('User-Agent', 'Unknown')
    }
    client_manager.register_client(client_id, client_info)
    
    # Get tasks for this client
    tasks = client_manager.get_client_tasks(client_id)
    
    # Also check database for queued tasks
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
        return ('', 204)  # No tasks right now
    
    return jsonify({'tasks': tasks}), 200

# Add to app.py
@app.route('/api/task-result', methods=['POST'])
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
        return jsonify({'error': 'Task not found'}), 404
    except Exception as e:
        logger.error(f"Error processing task result: {e}")
        return jsonify({'error': str(e)}), 500

# Add to app.py
@app.route('/api/register-client', methods=['POST'])
def api_register_client():
    """Register a new client"""
    try:
        data = request.json
        client_id = data.get('client_id')
        client_url = data.get('client_url')
        api_key = data.get('api_key')
        
        if not all([client_id, client_url, api_key]):
            return jsonify({'error': 'Missing required parameters'}), 400
            
        # Validate API key
        user = User.objects(gemini_api_key=api_key).first()
        if not user:
            return jsonify({'error': 'Invalid API key'}), 403
            
        # Register client
        client_manager.register_client(client_id, {
            'user_id': str(user.id),
            'client_url': client_url,
            'last_seen': datetime.utcnow(),
            'user_agent': request.headers.get('User-Agent', 'Unknown')
        })
        
        return jsonify({'success': True, 'message': 'Client registered successfully'})
        
    except Exception as e:
        logger.error(f"Error registering client: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/confirm_message_action', methods=['POST'])
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
        decision_data = {
            'campaign_id': campaign_id,
            'contact_index': contact_index,
            'action': action,
            'message': message,
            'timestamp': time.time()
        }
        
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
    

@app.route('/api/inbox_results', methods=['POST'])
def api_inbox_results():
    """
    Clients report task results here.
    """
    auth = request.headers.get('Authorization', '')
    api_key = None
    if auth.startswith('Bearer '):
        api_key = auth.replace('Bearer ', '').strip()
    else:
        api_key = (request.json or {}).get('api_key')

    if not api_key:
        return jsonify({'error': 'Missing API key'}), 401

    user = User.objects(gemini_api_key=api_key).first()
    if not user:
        return jsonify({'error': 'Invalid API key'}), 403

    payload = request.get_json() or {}
    task_id = payload.get('process_id') or payload.get('task_id')
    results = payload.get('results')


    # Store results — adapt to your DB/logic
    try:
        with open(f"reports/{task_id}.json", "w", encoding="utf-8") as f:
            json.dump({
                'user': str(user.id),
                'results': results,
                'received_at': datetime.utcnow().isoformat()
            }, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save results for {task_id}: {e}")

    return jsonify({'success': True}), 200


@app.route('/ai_inbox', methods=['GET', 'POST'])
@login_required
@linkedin_setup_required
def ai_inbox():
    user = get_current_user()
    
    if request.method == 'POST':
        try:
            # Check if local client is available
            if not client_manager.is_client_available(str(user.id)):
                flash('Local client not available. Please start the local client application.', 'error')
                return redirect(url_for('client_setup'))
            
            # Send request to local client
            result = client_manager.send_inbox_processing_request(str(user.id))
            
            if result.get('success'):
                flash('Inbox processing started on local client!', 'success')
                session['current_inbox_process'] = result.get('process_id')
            else:
                flash(f'Inbox processing failed: {result.get("error")}', 'error')
            
            return redirect(url_for('ai_inbox'))
            
        except Exception as e:
            flash(f'Inbox processing error: {str(e)}', 'error')
            return render_template('ai_inbox.html', user=user)
    
    # GET request - show status
    return render_template('ai_inbox.html', 
                         user=user,
                         client_available=client_manager.is_client_available(str(user.id)))

@app.route('/inbox_results/<inbox_id>')
@login_required
def get_inbox_results(inbox_id):
    results = inbox_results.get(inbox_id, {})
    return jsonify(results)
    
@app.route('/api/create-task', methods=['POST'])
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



@app.route('/api/campaign_progress', methods=['POST'])
def receive_campaign_progress():
    """Receive campaign progress from local client"""
    try:
        data = request.json
        campaign_id = data.get('campaign_id')
        progress = data.get('progress', {})
        is_final = data.get('final', False)
        
        # Store progress in campaign_results
        campaign_results[campaign_id] = progress
        
        if is_final:
            logger.info(f"✅ Campaign {campaign_id} completed")
        
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"❌ Error receiving campaign progress: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/search_results', methods=['POST'])
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

@app.route('/logout')
@login_required
def logout():
    user_name = session.get('user_name', '')
    session.clear()
    
    flash(f'Goodbye, {user_name}!', 'info')
    return redirect(url_for('login'))

@app.errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('500.html'), 500

if __name__ == '__main__':
    print(f"Starting Flask app...")
    print(f"Project directory: {basedir}")
    print(f"Upload folder: {app.config['UPLOAD_FOLDER']}")
    
    app.run(debug=True, host='0.0.0.0', port=5000)