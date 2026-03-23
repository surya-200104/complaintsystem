from gevent import monkey
monkey.patch_all()

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_socketio import SocketIO, emit, join_room
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from groq import Groq
from threading import Timer
from dotenv import load_dotenv
from sqlalchemy.pool import NullPool
import os

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret123'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///complaints.db').replace("postgres://", "postgresql://")
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'poolclass': NullPool}
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

os.makedirs('static/uploads', exist_ok=True)

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')
login_manager = LoginManager(app)
login_manager.login_view = 'login'

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def analyze_complaint_with_ai(title, description, category):
    try:
        prompt = f"""You are a complaint management AI assistant. Analyze this complaint and respond in EXACTLY this format with no extra text:

PRIORITY: High
ESTIMATED_TIME: 2-3 days
SUMMARY: One line summary here.
SUGGESTED_REPLY: Professional reply here.
INSIGHT: One line insight here.

Now analyze this complaint:
Title: {title}
Category: {category}
Description: {description}

Respond in EXACTLY the same format. Each field on a new line."""

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.choices[0].message.content.strip()
        print("AI RESPONSE:", text)

        result = {
            'priority': 'Medium',
            'estimated_time': '2-3 days',
            'summary': '',
            'suggested_reply': '',
            'insight': ''
        }

        for line in text.split('\n'):
            line = line.strip()
            if line.startswith('PRIORITY:'):
                result['priority'] = line.replace('PRIORITY:', '').strip()
            elif line.startswith('ESTIMATED_TIME:'):
                result['estimated_time'] = line.replace('ESTIMATED_TIME:', '').strip()
            elif line.startswith('SUMMARY:'):
                result['summary'] = line.replace('SUMMARY:', '').strip()
            elif line.startswith('SUGGESTED_REPLY:'):
                result['suggested_reply'] = line.replace('SUGGESTED_REPLY:', '').strip()
            elif line.startswith('INSIGHT:'):
                result['insight'] = line.replace('INSIGHT:', '').strip()

        print("PARSED RESULT:", result)
        return result

    except Exception as e:
        print(f"AI Error: {e}")
        return {
            'priority': 'Medium',
            'estimated_time': '2-3 days',
            'summary': 'AI analysis unavailable',
            'suggested_reply': 'Thank you for your complaint. We will look into it shortly.',
            'insight': 'Please review this complaint manually.'
        }

def check_duplicates(title, description):
    try:
        existing = Complaint.query.filter(
            Complaint.status != 'Resolved'
        ).all()
        if not existing:
            return None
        complaints_text = "\n".join([
            f"ID:{c.id} - {c.title}: {c.description[:100]}"
            for c in existing
        ])
        prompt = f"""You are a duplicate complaint detector. Check if the new complaint is a duplicate of any existing complaint.

New Complaint:
Title: {title}
Description: {description}

Existing Complaints:
{complaints_text}

If you find a duplicate, respond with ONLY: DUPLICATE:ID (example: DUPLICATE:5)
If no duplicate found, respond with ONLY: UNIQUE

Do not add any other text."""

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.choices[0].message.content.strip()
        print("DUPLICATE CHECK:", result)
        if result.startswith("DUPLICATE:"):
            dup_id = int(result.replace("DUPLICATE:", "").strip())
            return dup_id
        return None
    except Exception as e:
        print(f"Duplicate check error: {e}")
        return None

def check_escalations():
    with app.app_context():
        try:
            threshold = datetime.utcnow() - timedelta(days=3)
            complaints = Complaint.query.filter(
                Complaint.status == 'Pending',
                Complaint.date_posted <= threshold,
                Complaint.priority != 'Critical'
            ).all()
            for complaint in complaints:
                complaint.priority = 'Critical'
                db.session.commit()
                socketio.emit('escalation_alert', {
                    'id': complaint.id,
                    'title': complaint.title
                })
                print(f"Auto Escalated: {complaint.title}")
        except Exception as e:
            print(f"Escalation error: {e}")
    Timer(3600, check_escalations).start()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True)
    email = db.Column(db.String(100), unique=True)
    password = db.Column(db.String(200))
    phone = db.Column(db.String(20))
    is_admin = db.Column(db.Boolean, default=False)
    date_joined = db.Column(db.DateTime, default=datetime.utcnow)
    complaints = db.relationship('Complaint', backref='author', lazy=True)

class Complaint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    description = db.Column(db.Text)
    category = db.Column(db.String(100))
    priority = db.Column(db.String(50), default='Medium')
    status = db.Column(db.String(50), default='Pending')
    location = db.Column(db.String(100), default='Main Campus')
    attachment = db.Column(db.String(300))
    ai_summary = db.Column(db.Text)
    ai_estimated_time = db.Column(db.String(100))
    ai_suggested_reply = db.Column(db.Text)
    ai_insight = db.Column(db.Text)
    is_duplicate = db.Column(db.Boolean, default=False)
    duplicate_of = db.Column(db.Integer, nullable=True)
    rating = db.Column(db.Integer, nullable=True)
    feedback = db.Column(db.Text, nullable=True)
    date_posted = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    replies = db.relationship('Reply', backref='complaint', lazy=True)

class Reply(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.Text)
    date_posted = db.Column(db.DateTime, default=datetime.utcnow)
    is_admin = db.Column(db.Boolean, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    complaint_id = db.Column(db.Integer, db.ForeignKey('complaint.id'))
    user = db.relationship('User', backref='replies')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
def index():
    return render_template('landing.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        if User.query.filter_by(email=request.form['email']).first():
            flash('Email already registered!')
            return redirect(url_for('register'))
        user = User(
            username=request.form['username'],
            email=request.form['email'],
            phone=request.form['phone'],
            password=generate_password_hash(request.form['password'])
        )
        db.session.add(user)
        db.session.commit()
        flash('Registration successful! Please login.')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(email=request.form['email']).first()
        if user and check_password_hash(user.password, request.form['password']):
            login_user(user)
            return redirect(url_for('admin_dashboard') if user.is_admin else url_for('user_dashboard'))
        flash('Invalid email or password!')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/user/dashboard')
@login_required
def user_dashboard():
    complaints = Complaint.query.filter_by(user_id=current_user.id).order_by(Complaint.date_posted.desc()).all()
    total = len(complaints)
    pending = len([c for c in complaints if c.status == 'Pending'])
    in_progress = len([c for c in complaints if c.status == 'In Progress'])
    resolved = len([c for c in complaints if c.status == 'Resolved'])
    return render_template('user_dashboard.html', complaints=complaints,
                           total=total, pending=pending,
                           in_progress=in_progress, resolved=resolved)

@app.route('/submit', methods=['GET', 'POST'])
@login_required
def submit_complaint():
    if request.method == 'POST':
        filename = None
        if 'attachment' in request.files:
            file = request.files['attachment']
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

        title = request.form['title']
        description = request.form['description']
        category = request.form['category']
        priority = request.form['priority']
        location = request.form.get('location', 'Main Campus')

        dup_id = check_duplicates(title, description)
        ai = analyze_complaint_with_ai(title, description, category)

        complaint = Complaint(
            title=title,
            description=description,
            category=category,
            priority=priority,
            location=location,
            attachment=filename,
            ai_summary=ai['summary'],
            ai_estimated_time=ai['estimated_time'],
            ai_suggested_reply=ai['suggested_reply'],
            ai_insight=ai['insight'],
            is_duplicate=True if dup_id else False,
            duplicate_of=dup_id,
            user_id=current_user.id
        )
        db.session.add(complaint)
        db.session.commit()
        socketio.emit('new_complaint', {
            'title': complaint.title,
            'priority': complaint.priority,
            'user': current_user.username
        })
        if dup_id:
            flash(f'⚠️ Complaint submitted! AI detected a possible duplicate of Complaint #{dup_id}.')
        else:
            flash('Complaint submitted! AI has analyzed your complaint.')
        return redirect(url_for('user_dashboard'))
    return render_template('submit_complaint.html')

@app.route('/complaint/<int:id>')
@login_required
def complaint_detail(id):
    complaint = Complaint.query.get_or_404(id)
    if not current_user.is_admin and complaint.user_id != current_user.id:
        return redirect(url_for('user_dashboard'))
    replies = Reply.query.filter_by(complaint_id=id).order_by(Reply.date_posted.asc()).all()
    return render_template('complaint_detail.html', complaint=complaint, replies=replies)

@app.route('/complaint/<int:id>/reply', methods=['POST'])
@login_required
def add_reply(id):
    complaint = Complaint.query.get_or_404(id)
    reply = Reply(
        message=request.form['message'],
        is_admin=current_user.is_admin,
        user_id=current_user.id,
        complaint_id=id
    )
    db.session.add(reply)
    complaint.updated_at = datetime.utcnow()
    db.session.commit()
    socketio.emit('new_reply', {
        'complaint_id': id,
        'message': reply.message,
        'username': current_user.username,
        'is_admin': current_user.is_admin,
        'time': reply.date_posted.strftime('%d-%m-%Y %H:%M')
    }, room=f'complaint_{id}')
    return redirect(url_for('complaint_detail', id=id))

@app.route('/complaint/<int:id>/delete', methods=['POST'])
@login_required
def delete_complaint(id):
    complaint = Complaint.query.get_or_404(id)
    if complaint.user_id != current_user.id and not current_user.is_admin:
        return redirect(url_for('user_dashboard'))
    Reply.query.filter_by(complaint_id=id).delete()
    db.session.delete(complaint)
    db.session.commit()
    flash('Complaint deleted successfully!')
    return redirect(url_for('user_dashboard'))

@app.route('/complaint/<int:id>/feedback', methods=['POST'])
@login_required
def submit_feedback(id):
    complaint = Complaint.query.get_or_404(id)
    if complaint.user_id != current_user.id:
        return redirect(url_for('user_dashboard'))
    complaint.rating = int(request.form['rating'])
    complaint.feedback = request.form['feedback']
    db.session.commit()
    flash('Thank you for your feedback!')
    return redirect(url_for('complaint_detail', id=id))

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        return redirect(url_for('user_dashboard'))
    complaints = Complaint.query.order_by(Complaint.date_posted.desc()).all()
    total = len(complaints)
    pending = len([c for c in complaints if c.status == 'Pending'])
    in_progress = len([c for c in complaints if c.status == 'In Progress'])
    resolved = len([c for c in complaints if c.status == 'Resolved'])
    users = User.query.filter_by(is_admin=False).count()
    duplicates = Complaint.query.filter_by(is_duplicate=True).count()
    insights = [c.ai_insight for c in complaints if c.ai_insight]
    return render_template('admin_dashboard.html', complaints=complaints,
                           total=total, pending=pending,
                           in_progress=in_progress, resolved=resolved,
                           users=users, insights=insights,
                           duplicates=duplicates)

@app.route('/admin/update/<int:id>', methods=['POST'])
@login_required
def update_status(id):
    if not current_user.is_admin:
        return redirect(url_for('user_dashboard'))
    complaint = Complaint.query.get_or_404(id)
    complaint.status = request.form['status']
    complaint.updated_at = datetime.utcnow()
    db.session.commit()
    socketio.emit('status_update', {
        'id': id,
        'status': complaint.status
    })
    flash('Status updated successfully!')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/heatmap')
@login_required
def heatmap():
    if not current_user.is_admin:
        return redirect(url_for('user_dashboard'))
    complaints = Complaint.query.all()
    complaint_data = [{
        'id': c.id,
        'title': c.title,
        'location': c.location,
        'priority': c.priority,
        'status': c.status,
        'category': c.category
    } for c in complaints]
    return render_template('heatmap.html', complaints=complaint_data)

@app.route('/profile')
@login_required
def profile():
    complaints = Complaint.query.filter_by(user_id=current_user.id).all()
    return render_template('profile.html', complaints=complaints)

@socketio.on('join')
def on_join(data):
    room = data['room']
    join_room(room)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    check_escalations()
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, use_reloader=False)