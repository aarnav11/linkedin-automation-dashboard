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
@linkedin_setup_required
def start_campaign():
    try:
        user = get_current_user()
        campaign_data = session.get('current_campaign')
        
        if not campaign_data:
            return jsonify({'error': 'no campaign'}), 400
        
        cid = campaign_data['campaign_id']
        
        if automation_status['running']:
            return jsonify({'error': 'already running'}), 400

        # Reset flags
        campaign_controls[cid] = {'stop': False, 'action': None}
        automation_status.update({
            'running': True,
            'awaiting': False,
            'message': 'Initializing LinkedIn...',
            'progress': 0,
            'total': campaign_data['max_contacts'],
            'current': None
        })

        def worker():
            bot = None
            try:
                # Initialize LinkedIn automation
                logger.info("🚀 Starting LinkedIn automation")
                automation_status['message'] = 'Connecting to LinkedIn...'
                
                bot = LinkedInAutomation(
                    api_key=user.gemini_api_key,
                    email=user.linkedin_email,
                    password=user.get_linkedin_password()
                )
                
                # Login to LinkedIn
                automation_status['message'] = 'Logging into LinkedIn...'
                if not bot.login():
                    automation_status.update(
                        running=False, 
                        awaiting=False, 
                        message='LinkedIn login failed!'
                    )
                    return
                
                logger.info("✅ LinkedIn login successful")
                automation_status['message'] = 'LinkedIn connected successfully'
                
                # Initialize results
                results = {
                    'success': True,
                    'successful_contacts': 0,
                    'failed_contacts': 0,
                    'skipped_contacts': 0,
                    'already_messaged': 0,  # New counter
                    'contacts_processed': []
                }

                # Process contacts up to max_contacts limit
                contacts_to_process = campaign_data['contacts'][:campaign_data['max_contacts']]
                
                for idx, contact in enumerate(contacts_to_process):
                    if campaign_controls[cid]['stop']:
                        automation_status.update(
                            running=False, 
                            awaiting=False, 
                            message='Stopped by user'
                        )
                        break

                    try:
                        # Get LinkedIn URL
                        linkedin_url = contact.get('LinkedIn_profile', '')
                        if not linkedin_url or 'linkedin.com/in/' not in linkedin_url:
                            logger.warning(f"Invalid LinkedIn URL for {contact['Name']}")
                            results['failed_contacts'] += 1
                            automation_status['progress'] += 1
                            continue
                        
                        # **NEW: CHECK IF ALREADY MESSAGED**
                        if bot.is_profile_messaged(linkedin_url):
                            logger.info(f"⏭️ Skipping {contact['Name']} - already messaged previously")
                            results['already_messaged'] += 1
                            automation_status['progress'] += 1
                            
                            # Add to processed list
                            contact_result = {
                                'name': contact['Name'],
                                'company': contact['Company'],
                                'role': contact['Role'],
                                'linkedin_url': linkedin_url,
                                'message': 'Already messaged previously',
                                'success': False,
                                'method': 'already_messaged',
                                'timestamp': datetime.now().isoformat()
                            }
                            results['contacts_processed'].append(contact_result)
                            continue
                        
                        automation_status['message'] = f"Processing {contact['Name']}..."
                        logger.info(f"🌐 Navigating to {contact['Name']}'s profile")
                        
                        # Navigate to profile
                        bot.driver.get(linkedin_url)
                        time.sleep(3)  # Wait for page to load
                        
                        # Extract profile data
                        profile_data = bot.extract_profile_data()
                        
                        # Generate personalized message
                        automation_status['message'] = f"Generating message for {contact['Name']}..."
                        msg = bot.generate_message(
                            contact['Name'], 
                            contact['Company'], 
                            contact['Role'],
                            contact.get('services and products_1', ''),
                            contact.get('services and products_2', ''),
                            profile_data
                        )
                        
                        # Wait for user approval
                        automation_status.update(
                            awaiting=True,
                            current={**contact, 'message': msg},
                            message=f'Awaiting approval for {contact["Name"]} ({idx+1}/{len(contacts_to_process)})'
                        )
                        
                        # Wait for user decision
                        while not campaign_controls[cid]['action']:
                            if campaign_controls[cid]['stop']:
                                break
                            time.sleep(0.5)

                        action = campaign_controls[cid]['action']
                        campaign_controls[cid]['action'] = None  # Reset
                        automation_status['awaiting'] = False

                        if campaign_controls[cid]['stop']:
                            automation_status.update(
                                running=False,
                                message='Stopped by user'
                            )
                            break

                        # Process user decision
                        contact_result = {
                            'name': contact['Name'],
                            'company': contact['Company'],
                            'role': contact['Role'],
                            'linkedin_url': linkedin_url,
                            'message': msg,
                            'success': False,
                            'method': None,
                            'timestamp': datetime.now().isoformat()
                        }

                        if action == 'skip':
                            logger.info(f"⏭️ User skipped {contact['Name']}")
                            results['skipped_contacts'] += 1
                            contact_result['method'] = 'user_skipped'
                            automation_status['progress'] += 1
                            results['contacts_processed'].append(contact_result)
                            continue

                        # action == 'send'
                        automation_status['message'] = f"Sending connection request to {contact['Name']}..."
                        logger.info(f"🤝 Sending connection request to {contact['Name']}")
                        
                        # Try to send connection request
                        success = bot.send_connection_request_with_note(msg, contact['Name'])
                        
                        if success:
                            logger.info(f"✅ Connection request sent to {contact['Name']}")
                            results['successful_contacts'] += 1
                            contact_result['success'] = True
                            contact_result['method'] = 'connection_with_note'
                            
                            # **ADD TO TRACKING - This is the key part**
                            bot.add_profile_to_tracked(linkedin_url)
                            
                            # Human-like delay between successful sends
                            time.sleep(random.uniform(30, 60))
                            
                        else:
                            logger.error(f"❌ Failed to send connection request to {contact['Name']}")
                            results['failed_contacts'] += 1
                            contact_result['method'] = 'failed'

                        automation_status['progress'] += 1
                        results['contacts_processed'].append(contact_result)
                        
                    except Exception as e:
                        logger.error(f"❌ Error processing {contact['Name']}: {str(e)}")
                        results['failed_contacts'] += 1
                        automation_status['progress'] += 1
                        continue

                # Campaign completed
                automation_status.update(
                    running=False, 
                    awaiting=False,
                    message='Campaign completed successfully!'
                )
                
                campaign_results[cid] = results
                logger.info(f"✅ Campaign completed: {results['successful_contacts']} successful, {results['failed_contacts']} failed, {results['skipped_contacts']} user skipped, {results['already_messaged']} already messaged")
                
            except Exception as e:
                logger.error(f"💥 Campaign error: {str(e)}")
                automation_status.update(
                    running=False, 
                    awaiting=False,
                    message=f'Campaign error: {str(e)}'
                )
                campaign_results[cid] = {'success': False, 'error': str(e)}
                
            finally:
                if bot:
                    bot.close()

        # Start worker thread
        threading.Thread(target=worker, daemon=True).start()
        
        # Return response to Flask
        return jsonify({'success': True, 'campaign_id': cid})
        
    except Exception as e:
        logger.error(f"❌ Start campaign error: {str(e)}")
        return jsonify({'error': str(e)}), 500


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
    results = campaign_results.get(campaign_id, {})
    return jsonify(results)

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
        keywords = request.form.get('keywords', '').strip()
        max_invites = int(request.form.get('max_invites', 10))
        
        if not keywords:
            flash('Please enter keywords for search.', 'error')
            return render_template('keyword_search.html', user=user)
        
        try:
            # Initialize automation
            bot = LinkedInAutomation(
                email=user.linkedin_email,
                password=user.get_linkedin_password(),
                api_key=user.gemini_api_key
            )
            
            result = bot.search_and_connect(keywords=keywords, max_invites=max_invites)
            bot.driver.quit()  # Important: Close browser
            
            if isinstance(result, dict) and not result.get('success', True):
                flash(f"Error: {result.get('error')}", 'error')
            else:
                flash(f"Keyword search completed. Invitations sent: {result}", 'success')
                
            return render_template('keyword_search.html', user=user, keywords=keywords, invites_sent=result)
        
        except Exception as e:
            flash(f"Unexpected error during keyword search: {str(e)}", 'error')
            return render_template('keyword_search.html', user=user)
    
    return render_template('keyword_search.html', user=user)

@app.route('/search_results/<search_id>')
@login_required
def get_search_results(search_id):
    results = search_results_cache.get(search_id, {})
    return jsonify(results)

@app.route('/ai_inbox', methods=['GET', 'POST'])
@login_required
@linkedin_setup_required
def ai_inbox():
    user = get_current_user()
    
    if request.method == 'POST':
        try:
            # Generate inbox processing ID
            inbox_id = str(uuid.uuid4())
            
            def process_inbox():
                try:
                    automation_instance = LinkedInAutomation(
                        api_key=user.gemini_api_key,
                        email=user.linkedin_email,
                        password=user.get_linkedin_password()
                    )
                    
                    results = automation_instance.process_inbox_replies()
                    inbox_results[inbox_id] = results
                    
                except Exception as e:
                    logger.error(f"Inbox processing error: {e}")
                    inbox_results[inbox_id] = {'success': False, 'error': str(e)}
                finally:
                    if automation_instance:
                        automation_instance.close()
            
            # Start inbox processing in background
            inbox_thread = threading.Thread(target=process_inbox)
            inbox_thread.daemon = True
            inbox_thread.start()
            
            session['current_inbox_process'] = inbox_id
            flash('Inbox processing started! Check results below.', 'success')
            return redirect(url_for('ai_inbox'))
            
        except Exception as e:
            flash(f'Inbox processing error: {str(e)}', 'error')
            return render_template('ai_inbox.html', user=user)
    
    # Check for inbox results
    inbox_id = session.get('current_inbox_process')
    inbox_result = None
    
    if inbox_id:
        inbox_result = inbox_results.get(inbox_id)
    
    return render_template('ai_inbox.html', 
                         user=user,
                         inbox_result=inbox_result)

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
