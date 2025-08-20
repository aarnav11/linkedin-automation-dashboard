from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from datetime import datetime

db = SQLAlchemy()
bcrypt = Bcrypt()

class User(db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # LinkedIn Settings
    linkedin_email = db.Column(db.String(120), nullable=True)
    linkedin_password = db.Column(db.String(255), nullable=True)
    gemini_api_key = db.Column(db.String(255), nullable=True)
    
    def set_password(self, password):
        """Hash and set password"""
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
    
    def check_password(self, password):
        """Check if password matches hash"""
        return bcrypt.check_password_hash(self.password_hash, password)
    
    def set_linkedin_credentials(self, email, password, api_key):
        """Set LinkedIn credentials and API key"""
        self.linkedin_email = email
        self.linkedin_password = password
        self.gemini_api_key = api_key
        self._linkedin_password_plain = password
        #logger.info(f"Stored credentials for {email[:5]}***")

    def get_linkedin_password(self):
        """Get plain password for automation"""
        if hasattr(self, '_linkedin_password_plain'):
            return self._linkedin_password_plain
        return self.linkedin_password  # Return encrypted version as fallback

    def set_password_plain(self, password):
        """Temporarily store plain password for automation (not recommended for production)"""
        self._linkedin_password_plain = password

    def has_linkedin_setup(self):
        """Check if user has completed LinkedIn setup"""
        return bool(self.linkedin_email and self.linkedin_password and self.gemini_api_key)
    
    def get_full_name(self):
        """Get user's full name"""
        return f"{self.first_name} {self.last_name}"
    
    def to_dict(self):
        """Convert user to dictionary"""
        return {
            'id': self.id,
            'email': self.email,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'full_name': self.get_full_name(),
            'created_at': self.created_at.isoformat(),
            'has_linkedin_setup': self.has_linkedin_setup()
        }
    
    def __repr__(self):
        return f'<User {self.email}>'
