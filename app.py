from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import os
import pandas as pd
from werkzeug.utils import secure_filename
from models import db, User
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
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

campaign_results = {}
search_results_cache = {}
inbox_results = {}
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

# Fixed database configuration using absolute path
basedir = os.path.abspath(os.path.dirname(__file__))
instance_path = os.path.join(basedir, 'instance')
db_path = os.path.join(instance_path, 'users.db')

# Ensure instance directory exists
os.makedirs(instance_path, exist_ok=True)

# Database configuration with absolute path
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Initialize extensions
db.init_app(app)

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
        return User.query.get(session['user_id'])
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

# Flask 3.x compatible initialization
first_request = True

class LocalClientManager:
    def __init__(self):
        self.default_client_url = "http://127.0.0.1:5001"
        self.client_urls = {}  # user_id -> client_url mapping
    
    def register_client(self, user_id, client_url):
        """Register a client URL for a user"""
        self.client_urls[user_id] = client_url
        logger.info(f"✅ Registered client for user {user_id}: {client_url}")
    
    def get_client_url(self, user_id):
        """Get client URL for a user"""
        return self.client_urls.get(user_id, self.default_client_url)
    
    def is_client_available(self, user_id):
        """Check if local client is available"""
        client_url = self.get_client_url(user_id)
        try:
            response = requests.get(f"{client_url}/health", timeout=5)
            return response.status_code == 200
        except Exception:
            return False
    
    def send_campaign_request(self, user_id, campaign_data):
        """Send campaign request to local client"""
        client_url = self.get_client_url(user_id)
        try:
            user = User.query.get(user_id)
            payload = {
                'campaign_id': campaign_data['campaign_id'],
                'user_config': {
                    'linkedin_email': user.linkedin_email,
                    'linkedin_password': user.get_linkedin_password(),
                    'gemini_api_key': user.gemini_api_key
                },
                'campaign_data': campaign_data
            }
            
            response = requests.post(f"{client_url}/start_campaign", json=payload, timeout=10)
            return response.json()
        except Exception as e:
            logger.error(f"❌ Error sending campaign request: {e}")
            return {'success': False, 'error': str(e)}
    
    def send_keyword_search_request(self, user_id, search_params):
        """Send keyword search request to local client"""
        client_url = self.get_client_url(user_id)
        try:
            user = User.query.get(user_id)
            payload = {
                'search_id': str(uuid.uuid4()),
                'user_config': {
                    'linkedin_email': user.linkedin_email,
                    'linkedin_password': user.get_linkedin_password(),
                    'gemini_api_key': user.gemini_api_key
                },
                'search_params': search_params
            }
            
            response = requests.post(f"{client_url}/keyword_search", json=payload, timeout=10)
            return response.json()
        except Exception as e:
            logger.error(f"❌ Error sending keyword search request: {e}")
            return {'success': False, 'error': str(e)}
    
    def send_inbox_processing_request(self, user_id):
        """Send inbox processing request to local client"""
        client_url = self.get_client_url(user_id)
        try:
            user = User.query.get(user_id)
            payload = {
                'process_id': str(uuid.uuid4()),
                'user_config': {
                    'linkedin_email': user.linkedin_email,
                    'linkedin_password': user.get_linkedin_password(),
                    'gemini_api_key': user.gemini_api_key
                }
            }
            
            response = requests.post(f"{client_url}/process_inbox", json=payload, timeout=10)
            return response.json()
        except Exception as e:
            logger.error(f"❌ Error sending inbox processing request: {e}")
            return {'success': False, 'error': str(e)}
    
    def send_campaign_action(self, user_id, payload):
        """Send campaign action (send/skip/edit) to local client"""
        client_url = self.get_client_url(user_id)
        try:
            response = requests.post(f"{client_url}/campaign_action", json=payload, timeout=10)
            return response.json()
        except Exception as e:
            logger.error(f"❌ Error sending campaign action: {e}")
            return {'success': False, 'error': str(e)}

# Initialize the client manager
client_manager = LocalClientManager()

@app.route('/client_setup')
@login_required
def client_setup():
    """Show client setup instructions"""
    user = get_current_user()
    client_available = client_manager.is_client_available(user.id)
    
    return render_template('client_setup.html', 
                         user=user, 
                         client_available=client_available,
                         client_url=client_manager.get_client_url(user.id))

@app.route('/register_client', methods=['POST'])
@login_required
def register_client():
    """Register local client URL"""
    try:
        user = get_current_user()
        client_url = request.json.get('client_url', 'http://127.0.0.1:5001')
        
        # Validate URL format
        parsed = urlparse(client_url)
        if not parsed.scheme or not parsed.netloc:
            return jsonify({'success': False, 'error': 'Invalid URL format'}), 400
        
        # Test connection
        try:
            response = requests.get(f"{client_url}/health", timeout=5)
            if response.status_code != 200:
                return jsonify({'success': False, 'error': 'Client not responding'}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': f'Cannot connect to client: {e}'}), 400
        
        # Register client
        client_manager.register_client(user.id, client_url)
        
        return jsonify({'success': True, 'message': 'Client registered successfully'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/check_client_status')
@login_required
def check_client_status():
    """Check if local client is available"""
    user = get_current_user()
    available = client_manager.is_client_available(user.id)
    return jsonify({'available': available})

@app.before_request
def before_first_request():
    """Create database tables on first request"""
    global first_request
    if first_request:
        with app.app_context():
            db.create_all()
            print(f"Database initialized at: {db_path}")
        first_request = False

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
            existing_user = User.query.filter_by(email=email).first()
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
            
            db.session.add(user)
            db.session.commit()
            
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
            
        except Exception as e:
            db.session.rollback()
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
            user = User.query.filter_by(email=email).first()
            
            if user and user.check_password(password):
                # Login successful
                session['user_id'] = user.id
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

            # Save to user model
            user.linkedin_email = linkedin_email
            user.linkedin_password = linkedin_password  # optional: hash this
            user.gemini_api_key = gemini_api_key

            # ✅ Save password in memory (just for this session)
            user.set_password_plain(linkedin_password)

            db.session.commit()
            flash("Settings updated", "success")
            return redirect(url_for('settings'))

        except Exception as e:
            db.session.rollback()
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

@app.route('/start_campaign', methods=['POST'])
@login_required
def start_campaign():
    try:
        user = get_current_user()
        campaign_id = request.json.get('campaign_id')
        
        campaign_data = session.get('current_campaign')
        if not campaign_data or campaign_data.get('campaign_id') != campaign_id:
            return jsonify({'error': 'Campaign not found in session'}), 404

        if not client_manager.is_client_available(user.id):
            return jsonify({
                'error': 'Local client not available. Please start it.',
                'redirect': url_for('client_setup')
            }), 400

        # Enhanced campaign data with confirmation settings
        enhanced_campaign_data = campaign_data.copy()
        enhanced_campaign_data.update({
            'requires_confirmation': True,  # Enable preview mode
            'max_contacts_per_batch': 1,   # Process one contact at a time
            'confirmation_timeout': 300,   # 5 minutes per contact decision
            'auto_skip_timeout': True,     # Skip if no decision made
            'priority_order': ['connection_with_note', 'connection_without_note', 'direct_message']
        })

        result = client_manager.send_campaign_request(user.id, enhanced_campaign_data)
        
        if result.get('success'):
            return jsonify(result)
        else:
            return jsonify(result), 500

    except Exception as e:
        logger.error(f"❌ Start campaign error: {str(e)}")
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

    result = client_manager.send_campaign_action(user.id, payload)
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
    # This route now gets campaign status directly from the client bot
    # to ensure real-time data, including the 'awaiting_action' state.
    user = get_current_user()
    client_url = client_manager.get_client_url(user.id)
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

@app.route('/keyword', methods=['GET', 'POST'])
@login_required
@linkedin_setup_required
def keyword():
    user = get_current_user()
    
    if request.method == 'POST':
        try:
            search_keywords = request.form.get('keywords', '').strip()
            location = request.form.get('location', '').strip()
            search_type = request.form.get('search_type', 'search_only')
            max_invites = int(request.form.get('max_invites', 10))

            if not search_keywords:
                flash('Please enter search keywords!', 'error')
                return render_template('keyword.html', user=user)
            
            # Generate search ID
            search_id = str(uuid.uuid4())
            
            def run_search():
                automation_instance = None
                try:
                    automation_instance = LinkedInAutomation(
                        api_key=user.gemini_api_key,
                        email=user.linkedin_email,
                        password=user.get_linkedin_password()
                    )
                    
                    if search_type == 'search_only':
                        # Just search for profiles
                        results = automation_instance.search_profiles(
                            keywords=search_keywords,
                            location=location,
                            max_invites=max_invites
                        )
                        search_results_cache[search_id] = {
                            'type': 'search_only',
                            'results': results,
                            'keywords': search_keywords,
                            'location': location,
                            'timestamp': datetime.now().isoformat()
                        }
                    else:
                        # Search and connect with enhanced functionality
                        results = automation_instance.search_and_connect(
                            keywords=search_keywords,
                            max_invites=max_invites
                        )
                        search_results_cache[search_id] = {
                            'type': 'search_and_connect',
                            'results': results,
                            'keywords': search_keywords,
                            'location': location,
                            'timestamp': datetime.now().isoformat()
                        }
                        
                except Exception as e:
                    logger.error(f"Search error: {e}")
                    search_results_cache[search_id] = {
                        'error': str(e),
                        'timestamp': datetime.now().isoformat()
                    }
                finally:
                    if automation_instance:
                        automation_instance.close()
            
            # Start search in background
            search_thread = threading.Thread(target=run_search)
            search_thread.daemon = True
            search_thread.start()
            
            # Store search info in session
            session['current_search'] = {
                'search_id': search_id,
                'keywords': search_keywords,
                'location': location,
                'search_type': search_type,
                'max_invites': max_invites
            }
            
            flash('Search started! Results will appear shortly.', 'success')
            return redirect(url_for('keyword'))
            
        except Exception as e:
            flash(f'Search error: {str(e)}', 'error')
            return render_template('keyword.html', user=user)
    
    # Check for search results
    search_info = session.get('current_search')
    search_results = None
    
    if search_info:
        search_id = search_info['search_id']
        search_results = search_results_cache.get(search_id)
    
    return render_template('keyword.html', 
                         user=user,
                         search_info=search_info,
                         search_results=search_results)

@app.route('/keyword_search', methods=['GET', 'POST'])
@login_required
@linkedin_setup_required
def keyword_search():
    user = get_current_user()
    
    if request.method == 'POST':
        try:
            search_keywords = request.form.get('keywords', '').strip()
            location = request.form.get('location', '').strip()
            search_type = request.form.get('search_type', 'search_only')
            max_invites = int(request.form.get('max_invites', 10))

            if not search_keywords:
                flash('Please enter search keywords!', 'error')
                return render_template('keyword_search.html', user=user)
            
            # Check if local client is available
            if not client_manager.is_client_available(user.id):
                flash('Local client not available. Please start the local client application.', 'error')
                return redirect(url_for('client_setup'))
            
            search_params = {
                'keywords': search_keywords,
                'location': location,
                'search_type': search_type,
                'max_invites': max_invites
            }
            
            # Send request to local client
            result = client_manager.send_keyword_search_request(user.id, search_params)
            
            if result.get('success'):
                flash('Keyword search started on local client!', 'success')
                session['current_search'] = {
                    'search_id': result.get('search_id'),
                    'keywords': search_keywords,
                    'location': location,
                    'search_type': search_type,
                    'max_invites': max_invites
                }
            else:
                flash(f'Search failed: {result.get("error")}', 'error')
            
            return redirect(url_for('keyword_search'))
            
        except Exception as e:
            flash(f'Search error: {str(e)}', 'error')
            return render_template('keyword_search.html', user=user)
    
    # GET request - show form and any previous search results
    search_info = session.get('current_search')
    return render_template('keyword_search.html', 
                         user=user,
                         search_info=search_info,
                         client_available=client_manager.is_client_available(user.id))

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
        client_url = client_manager.get_client_url(user.id)
        
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
        
        result = client_manager.send_campaign_action(user.id, payload)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/ai_inbox', methods=['GET', 'POST'])
@login_required
@linkedin_setup_required
def ai_inbox():
    user = get_current_user()
    
    if request.method == 'POST':
        try:
            # Check if local client is available
            if not client_manager.is_client_available(user.id):
                flash('Local client not available. Please start the local client application.', 'error')
                return redirect(url_for('client_setup'))
            
            # Send request to local client
            result = client_manager.send_inbox_processing_request(user.id)
            
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
                         client_available=client_manager.is_client_available(user.id))

@app.route('/inbox_results/<inbox_id>')
@login_required
def get_inbox_results(inbox_id):
    results = inbox_results.get(inbox_id, {})
    return jsonify(results)

@app.route('/api/get-tasks', methods=['POST'])
def get_tasks():
    data = request.get_json()
    email = data.get('email')
    api_key = data.get('api_key')

    # Optional: validate email/API key
    # Lookup tasks in DB (or return static test data for now)
    sample_tasks = [
        {
            "profile_url": "https://www.linkedin.com/in/example123",
            "message": "Hi! This is a test message from your automation bot."
        }
    ]
    return jsonify({"tasks": sample_tasks})
    
@app.route('/tasks', methods=['POST'])
def tasks():
    data = request.json or {}
    email = data.get('email')
    api_key = data.get('api_key')
    # TODO: validate email/api_key against your User model
    # For now, just return some dummy tasks or pull from your campaign_results
    tasks = []
    for cid, results in campaign_results.items():
        for contact in results.get('contacts_processed', []):
            if not contact['success']:
                continue
            tasks.append({
                'profile_url': contact['linkedin_url'],
                'message': contact['message']
            })
    return jsonify(tasks=tasks)
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

@app.route('/api/inbox_results', methods=['POST'])
def receive_inbox_results():
    """Receive inbox processing results from local client"""
    try:
        data = request.json
        process_id = data.get('process_id')
        results = data.get('results', {})
        
        # Store results in inbox_results
        inbox_results[process_id] = results
        
        logger.info(f"✅ Received inbox results for {process_id}")
        return jsonify({'success': True})
        
    except Exception as e:
        logger.error(f"❌ Error receiving inbox results: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# In app.py

@app.route('/api/register_client_bot', methods=['POST'])
def register_client_bot():
    """
    Unauthenticated endpoint for the client bot to register itself.
    Authentication is done via a shared secret (Gemini API Key).
    """
    try:
        data = request.json
        client_url = data.get('client_url')
        gemini_key = data.get('gemini_api_key')

        if not client_url or not gemini_key:
            return jsonify({'success': False, 'error': 'Missing client_url or gemini_api_key'}), 400

        # Find the user by their unique Gemini API key
        user = User.query.filter_by(gemini_api_key=gemini_key).first()

        if not user:
            return jsonify({'success': False, 'error': 'Invalid API key. User not found.'}), 403

        # Test the provided client URL to ensure it's reachable
        try:
            logger.info(f"🧪 Testing connection to {client_url}...")
            response = requests.get(f"{client_url}/health", timeout=30)  # Increased timeout to 30 seconds
            if response.status_code == 200:
                logger.info("✅ Client connection test successful")
            else:
                logger.warning(f"⚠️ Client responded with status {response.status_code}")
        except requests.exceptions.RequestException as e:
            return jsonify({'success': False, 'error': f'Cannot connect to client: {e}'}), 400

        # Register the client URL for the found user
        client_manager.register_client(user.id, client_url)
        logger.info(f"✅ Successfully registered client bot for user {user.email} with URL: {client_url}")
        
        return jsonify({'success': True, 'message': 'Client registered successfully'})

    except Exception as e:
        logger.error(f"❌ Error during client bot registration: {e}")
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
    db.session.rollback()
    return render_template('500.html'), 500

if __name__ == '__main__':
    print(f"Starting Flask app...")
    print(f"Project directory: {basedir}")
    print(f"Database path: {db_path}")
    print(f"Upload folder: {app.config['UPLOAD_FOLDER']}")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
