from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response
import mysql.connector
from xhtml2pdf import pisa
from io import BytesIO

app = Flask(__name__)
app.secret_key = 'blood_bank_secret_key'

# ---- Database Connection ----
def get_db():
    """Create and return a database connection."""
    conn = mysql.connector.connect(
        host='localhost',
        user='root',
        password='nayan1214',
        database='blood_bank'
    )
    return conn


def update_stock(cursor, blood_group, units):
    """Safely update stock levels for a blood group."""
    cursor.execute("UPDATE inventory SET units = %s WHERE blood_group = %s", (units, blood_group))

def process_pending_requests(cursor):
    """Check and approve pending requests if inventory is available."""
    # Get all pending requests ordered by ID (oldest first)
    cursor.execute("SELECT * FROM blood_requests WHERE status='Pending' ORDER BY request_id ASC")
    pending_requests = cursor.fetchall()

    for request in pending_requests:
        bg = request['blood_group']
        req_units = request['units_required']
        req_id = request['request_id']

        # Check current stock
        cursor.execute("SELECT units FROM inventory WHERE blood_group=%s", (bg,))
        stock = cursor.fetchone()

        if stock and stock['units'] >= req_units:
            # Approve and reduce
            new_stock = stock['units'] - req_units
            update_stock(cursor, bg, new_stock)
            cursor.execute("UPDATE blood_requests SET status='Approved' WHERE request_id=%s", (req_id,))
            # Note: No commit here, handled by the calling route for atomicity

# ---- LOGIN ----
@app.route('/')
def home():
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    # Check for logout message from query parameter
    logout_msg = request.args.get('msg') == 'logged_out'
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM admin WHERE username=%s AND password=%s", (username, password))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user:
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('dashboard'))
        else:
            error = "Invalid username or password!"

    return render_template('login.html', error=error, logout_msg=logout_msg)


# ---- LOGOUT ----
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login', msg='logged_out'))


# ---- DASHBOARD ----
@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    # Total donors
    cursor.execute("SELECT COUNT(*) AS total FROM donors")
    total_donors = cursor.fetchone()['total']

    # Total blood units
    cursor.execute("SELECT SUM(units) AS total FROM inventory")
    result = cursor.fetchone()
    total_units = result['total'] if result['total'] else 0

    # Total requests
    cursor.execute("SELECT COUNT(*) AS total FROM blood_requests")
    total_requests = cursor.fetchone()['total']

    # Recent Donations
    cursor.execute("""
        SELECT d.donation_id, don.name AS donor_name, d.blood_group, d.units, d.donation_date
        FROM donations d
        JOIN donors don ON d.donor_id = don.donor_id
        ORDER BY d.donation_id DESC
        LIMIT 5
    """)
    recent_donations = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('dashboard.html',
                           total_donors=total_donors,
                           total_units=total_units,
                           total_requests=total_requests,
                           recent_donations=recent_donations)


# ---- DONORS MODULE ----
@app.route('/donors', methods=['GET', 'POST'])
def donors():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    if request.method == 'POST':
        name = request.form['name']
        dob_str = request.form['dob']
        # Calculate age from date of birth
        from datetime import datetime, date
        dob_date = datetime.strptime(dob_str, '%Y-%m-%d').date()
        today = date.today()
        age = today.year - dob_date.year - ((today.month, today.day) < (dob_date.month, dob_date.day))
        gender = request.form['gender']
        blood_group = request.form['blood_group']
        phone = request.form['phone']

        cursor.execute(
            "INSERT INTO donors (name, dob, age, gender, blood_group, phone) VALUES (%s, %s, %s, %s, %s, %s)",
            (name, dob_str, age, gender, blood_group, phone)
        )
        conn.commit()
        flash('Donor added successfully!', 'success')

    cursor.execute("SELECT * FROM donors ORDER BY donor_id ASC")
    all_donors = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template('donors.html', donors=all_donors)


@app.route('/delete_donor/<int:donor_id>')
def delete_donor(donor_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor()

    # Delete related donations first
    cursor.execute("DELETE FROM donations WHERE donor_id=%s", (donor_id,))
    cursor.execute("DELETE FROM donors WHERE donor_id=%s", (donor_id,))
    conn.commit()
    cursor.close()
    conn.close()

    flash('Donor deleted successfully!', 'warning')
    return redirect(url_for('donors'))


@app.route('/donor_card/<int:donor_id>')
def donor_card(donor_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM donors WHERE donor_id = %s", (donor_id,))
    donor = cursor.fetchone()
    cursor.close()
    conn.close()

    if not donor:
        flash('Donor not found.', 'danger')
        return redirect(url_for('donors'))

    return render_template('donor_card.html', donor=donor)


# ---- INVENTORY MODULE ----
@app.route('/inventory', methods=['GET', 'POST'])
def inventory():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    if request.method == 'POST':
        blood_group = request.form['blood_group']
        adjustment = int(request.form['adjustment'])
        reason = request.form.get('reason', 'Manual Adjustment')

        # Get current units
        cursor.execute("SELECT units FROM inventory WHERE blood_group=%s", (blood_group,))
        curr = cursor.fetchone()
        
        if curr:
            new_total = curr['units'] + adjustment
            # Prevent negative inventory
            if new_total < 0:
                new_total = 0
            
            update_stock(cursor, blood_group, new_total)
            
            # If we added stock, check if we can fulfill pending requests
            if adjustment > 0:
                process_pending_requests(cursor)
                
            conn.commit()
            flash(f'Stock adjusted successfully! ({"+" if adjustment >= 0 else ""}{adjustment} units)', 'success')

    cursor.execute("SELECT * FROM inventory ORDER BY FIELD(blood_group, 'A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-')")
    all_inventory = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template('inventory.html', inventory=all_inventory)


# ---- REQUESTS MODULE ----
@app.route('/requests', methods=['GET', 'POST'])
def requests_page():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    if request.method == 'POST':
        patient_name = request.form['patient_name']
        hospital_name = request.form['hospital_name']
        blood_group = request.form['blood_group']
        units_required = int(request.form['units_required'])

        # Initial check
        cursor.execute("SELECT units FROM inventory WHERE blood_group=%s", (blood_group,))
        stock = cursor.fetchone()

        if stock and stock['units'] >= units_required:
            new_units = stock['units'] - units_required
            update_stock(cursor, blood_group, new_units)
            status = 'Approved'
            flash('Request approved! Stock updated.', 'success')
        else:
            status = 'Pending'
            flash('Not enough stock. Request is pending.', 'warning')

        cursor.execute(
            "INSERT INTO blood_requests (patient_name, hospital_name, blood_group, units_required, status) VALUES (%s, %s, %s, %s, %s)",
            (patient_name, hospital_name, blood_group, units_required, status)
        )
        conn.commit()

    cursor.execute("SELECT * FROM blood_requests ORDER BY request_id ASC")
    all_requests = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template('requests.html', requests=all_requests)


# ---- DONATIONS MODULE ----
@app.route('/donations', methods=['GET', 'POST'])
def donations():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    if request.method == 'POST':
        donor_id = request.form['donor_id']
        units = int(request.form['units'])

        cursor.execute("SELECT blood_group FROM donors WHERE donor_id=%s", (donor_id,))
        donor = cursor.fetchone()
        if not donor:
            flash('Error: Donor not found.', 'danger')
            return redirect(url_for('donations'))
        
        blood_group = donor['blood_group']

        # Add donation record
        cursor.execute(
            "INSERT INTO donations (donor_id, blood_group, units) VALUES (%s, %s, %s)",
            (donor_id, blood_group, units)
        )
        new_donation_id = cursor.lastrowid

        # Increase inventory using shared logic
        cursor.execute("SELECT units FROM inventory WHERE blood_group=%s", (blood_group,))
        curr_stock = cursor.fetchone()
        new_units = curr_stock['units'] + units
        update_stock(cursor, blood_group, new_units)
        
        process_pending_requests(cursor)
        conn.commit()
        flash('Donation recorded successfully!', 'success')
        return redirect(url_for('donation_receipt', donation_id=new_donation_id))

    # Get all donations with donor names
    cursor.execute("""
        SELECT d.donation_id, d.donor_id, don.name AS donor_name,
               d.blood_group, d.units, d.donation_date
        FROM donations d
        JOIN donors don ON d.donor_id = don.donor_id
        ORDER BY d.donation_id ASC
    """)
    all_donations = cursor.fetchall()

    # Get all donors for dropdown
    cursor.execute("SELECT donor_id, name, blood_group FROM donors ORDER BY name")
    all_donors = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('donations.html', donations=all_donations, donors=all_donors)


@app.route('/donor_donations/<int:donor_id>')
def donor_donations(donor_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    # Get donor info
    cursor.execute("SELECT * FROM donors WHERE donor_id = %s", (donor_id,))
    donor = cursor.fetchone()

    if not donor:
        flash('Donor not found.', 'danger')
        return redirect(url_for('donors'))

    # Get their donations
    cursor.execute("""
        SELECT d.donation_id, d.units, d.blood_group, d.donation_date
        FROM donations d
        WHERE d.donor_id = %s
        ORDER BY d.donation_id DESC
    """, (donor_id,))
    donations = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('donor_donations.html', donor=donor, donations=donations)


@app.route('/donation_receipt/<int:donation_id>')
def donation_receipt(donation_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("""
        SELECT d.donation_id, d.units, d.blood_group, d.donation_date, 
               don.name AS donor_name, don.phone, don.age, don.gender
        FROM donations d
        JOIN donors don ON d.donor_id = don.donor_id
        WHERE d.donation_id = %s
    """, (donation_id,))
    donation = cursor.fetchone()
    
    cursor.close()
    conn.close()

    if not donation:
        flash('Receipt not found.', 'danger')
        return redirect(url_for('donations'))

    return render_template('receipt.html', donation=donation)

@app.route('/download_pdf/<int:donation_id>')
def download_pdf(donation_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("""
        SELECT d.donation_id, d.units, d.blood_group, d.donation_date, 
               don.name AS donor_name, don.phone, don.age, don.gender
        FROM donations d
        JOIN donors don ON d.donor_id = don.donor_id
        WHERE d.donation_id = %s
    """, (donation_id,))
    donation = cursor.fetchone()
    
    cursor.close()
    conn.close()

    if not donation:
        flash('Receipt not found.', 'danger')
        return redirect(url_for('donations'))

    rendered_html = render_template('receipt_pdf.html', donation=donation)
    
    pdf_buffer = BytesIO()
    pisa_status = pisa.CreatePDF(rendered_html, dest=pdf_buffer)
    
    if pisa_status.err:
        flash('Error generating PDF', 'danger')
        return redirect(url_for('donations'))
        
    pdf_buffer.seek(0)
    
    response = make_response(pdf_buffer.read())
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = f'attachment; filename=Donation_Slip_{donation_id}.pdf'
    return response


# ---- RUN APP ----
if __name__ == '__main__':
    app.run(debug=True)
