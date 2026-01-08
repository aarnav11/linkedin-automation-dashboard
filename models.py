from mongoengine import Document, IntField, StringField, DateTimeField, DictField, BooleanField, ReferenceField, ListField
from flask_bcrypt import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import uuid


class User(Document):
    meta = {'collection': 'users'}

    email = StringField(required=True, unique=True, max_length=120)
    password_hash = StringField(required=True, max_length=128)
    first_name = StringField(required=True, max_length=50)
    last_name = StringField(required=True, max_length=50)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)
    total_connections = IntField(default=0)
    initial_connections = IntField(null=True)
    google_refresh_token = StringField()
    google_scopes = ListField(StringField())
    # HubSpot Settings
    hubspot_access_token = StringField()
    hubspot_refresh_token = StringField()
    hubspot_token_expires_at = DateTimeField()
    hubspot_portal_id = StringField()
    # LinkedIn Settings
    linkedin_email = StringField(max_length=120)
    linkedin_password = StringField(max_length=255)
    gemini_api_key = StringField(max_length=255)

    # Temporary plain password storage for LinkedIn automation (not recommended for production)
    _linkedin_password_plain = None
    subscription_status = StringField(default='trial', required=True)
    subscription_ends_at = DateTimeField(default=lambda: datetime.utcnow() + timedelta(days=60))
    #stripe_customer_id = StringField()
    meta = {
        'collection': 'users'
    }

    def set_password(self, password):
        """Hash and set password"""
        self.password_hash = generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        """Check if password matches hash"""
        return check_password_hash(self.password_hash, password)

    def set_linkedin_credentials(self, email, password, api_key):
        """Set LinkedIn credentials and API key"""
        self.linkedin_email = email
        self.linkedin_password = password
        self.gemini_api_key = api_key
        self._linkedin_password_plain = password

    def get_linkedin_password(self):
        """Get plain password for automation"""
        if self._linkedin_password_plain:
            return self._linkedin_password_plain
        return self.linkedin_password  # Return stored version

    def set_password_plain(self, password):
        """Temporarily store plain password for automation"""
        self._linkedin_password_plain = password

    def has_linkedin_setup(self):
        """Check if user has completed LinkedIn setup"""
        return bool(self.linkedin_email and self.linkedin_password and self.gemini_api_key)

    def get_full_name(self):
        """Get user's full name"""
        return f"{self.first_name} {self.last_name}"

    def is_subscription_active(self):
        """Check if user has time left (Trial OR Paid)"""
        # If status is 'active' (paid), they are good
        if self.subscription_status == 'active':
            return True
            
        # If trial, check dates
        if self.subscription_ends_at and self.subscription_ends_at > datetime.utcnow():
            return True
            
        return False

    def to_dict(self):
        """Convert user to dictionary"""
        return {
            'id': str(self.id),  # MongoDB ID; convert to string
            'email': self.email,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'full_name': self.get_full_name(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'has_linkedin_setup': self.has_linkedin_setup()
        }

    def __repr__(self):
        return f"<User {self.email}>"
    

# In models.py, update the Task model
class Task(Document):
    meta = {'collection': 'tasks'}
    
    user = ReferenceField(User, required=True)
    task_type = StringField(required=True)  # outreach_campaign, profile_collection, etc.
    status = StringField(required=True, default='queued')  # queued, processing, completed, failed
    params = DictField()
    result = DictField()
    created_at = DateTimeField(default=datetime.utcnow)
    started_at = DateTimeField()
    completed_at = DateTimeField()
    error = StringField()
    
    def to_dict(self):
        return {
            'id': str(self.id),
            'type': self.task_type,
            'status': self.status,
            'params': self.params,
            'result': self.result,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'error': self.error
        }
    def __repr__(self):
        return f"<Task {self.id} type={self.task_type} user={self.user.email}>"

class Payment(Document):
    meta = {'collection': 'payments'}

    user = ReferenceField(User, required=True)
    razorpay_order_id = StringField(required=True)
    razorpay_payment_id = StringField()
    razorpay_signature = StringField()
    amount = IntField(required=True)  # Stored in smallest currency unit (paise)
    currency = StringField(default='USD')
    status = StringField(default='created') # created, paid, failed
    created_at = DateTimeField(default=datetime.utcnow)
    
    def __repr__(self):
        return f"<Payment {self.razorpay_order_id} - {self.status}>"