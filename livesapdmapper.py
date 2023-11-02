import hashlib
import psycopg2
import requests
import logging
import argparse
import os
import binascii
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template, request, session
from waitress import serve
from geopy.exc import GeocoderTimedOut
from geopy.geocoders import Nominatim

use_database = True  # Set this to False if you don't want to use the PostgreSQL database
user_host = "127.0.0.1"
user_port = "5012"
db_username = "postgres"
db_password = "postgres1"
db_host = "127.0.0.1"
db_port = "5432"
args = None
# Start of DataHandler class
class DataHandler:
    has_db_data_loaded = False
    parser = argparse.ArgumentParser(description='Live SAPD Calls For Service Server')
    parser.add_argument("--debugmode", help="Shows additional debug info in the terminal.", nargs='?', const=1, type=int, default=0)
    parser.add_argument("--infomode", help="Shows additional connection info in the terminal.", nargs='?', const=1, type=int, default=0)
    parser.add_argument("--prevhours", help="Show calls from the past X amount of hours.", type=int, default=24)
    parser.add_argument("--host", help="Hosts at 0.0.0.0 so other PCs on your Local Network can connect to this PC..", nargs='?', const=1, type=int, default=0)

    args = parser.parse_args()

    def __init__(self, use_database):
        self.use_database = use_database
        self.local_data = []
        self.sessions = {}
        self.url = "https://webapp3.sanantonio.gov/policecalls/Calls.aspx"
        if self.use_database:
            try:
                self.psqlConn= psycopg2.connect(dbname='livesapdcallsmap', user=db_username, password=db_password, host=db_host, port=db_port)
                self.psqlCursor = self.psqlConn.cursor()
            except psycopg2.Error as e:
                print(f"An error occurred while connecting to the database: {e}")
                print(f"Disabling database feature")
                use_database = False
                self.use_database = False
            pass

    def get_session(self, user_id):
        if user_id not in self.sessions:
            self.sessions[user_id] = {
                'session': requests.Session(),
                'viewstate': None,
                'eventvalidation': None,
                'viewstategenerator': None
            }
        return self.sessions[user_id]

    def fetch_data_from_database(self):
        time_hours_ago = datetime.now() - timedelta(weeks=0, days=0, hours=self.args.prevhours, minutes=0)
        time_hours_ago_str = time_hours_ago.strftime('%m/%d/%Y %I:%M:%S %p')
        user_id_short = self.print_user_id("short")
        user_id = self.print_user_id("full")
        if user_id not in self.sessions:
            self.get_session(user_id)
        print(f"user {user_id_short} connected.")
        if 'new_data' not in session:
            session['new_data'] = []
        print(f"Please wait to retrieve service call data.")
        if self.args.debugmode:
            print(f"Debugmode: fetch_data_from_db added_calls = {session['new_data']}.\n\n")
        if self.use_database:
            self.psqlCursor.execute("SELECT * FROM calls WHERE TO_TIMESTAMP(calldatetime, 'MM/DD/YYYY HH12:MI:SS AM') > TO_TIMESTAMP(%s, 'MM/DD/YYYY HH12:MI:SS AM');", (time_hours_ago_str,))
            rows = self.psqlCursor.fetchall()
            for row in rows:
                url_location, incident_number, calldatetime, problem_type, address, division, latitude, longitude = row
                self.local_data.append({
                    'incident_number': incident_number,
                    'calldatetime': calldatetime,
                    'problem_type': problem_type,
                    'address': address,
                    'division': division,
                    'latitude': latitude,
                    'longitude': longitude
                })
            self.local_data.sort(key=lambda x: datetime.strptime(x['calldatetime'], '%m/%d/%Y %I:%M:%S %p'), reverse=True)
            self.has_db_data_loaded = True

        result = {
            'data': self.local_data if self.use_database else [],
            'time_str': time_hours_ago_str,
            'prev_hours': self.args.prevhours,
            'user_session_id': user_id
        }

        print(f"Fetched previous data.")
        return jsonify(result)


    def get_data(self):
        user_id = self.print_user_id("full")
        session_info = self.get_session(user_id)
        print(f"Starting get data")
        user_id_short = self.print_user_id("short")
        new_call_found = False
        calls = self.get_locations(user_id)
        for call in calls:
            location = (call['location'].replace('+', ', ')) + " San Antonio, Texas"
            loc = self.do_geocode(location)
            if loc:
                incident_number = call['incident_number']
                is_duplicate = any(item['incident_number'] == incident_number for item in session['new_data'])
                if not self.use_database:
                    if not is_duplicate:
                        new_call_found = True
                        if self.args.debugmode:
                            print(f"\nDebugmode: get_data new_data before append: {session['new_data']}")
                        session['new_data'].append({
                            'url_location': call['location'],
                            'incident_number': incident_number,                    
                            'calldatetime': call['calldatetime'],
                            'problem_type': call['problem_type'],
                            'address': call['address'],
                            'division': call['division'],
                            'latitude': loc.latitude,  
                            'longitude': loc.longitude
                        })
                        session.modified = True  # Notify Flask that the session has been modified
                        callstring = (f"{call['calldatetime']}: Call for Service {incident_number} at {call['address']} added for user {user_id_short}.")
                        print(callstring.replace("\n", ""))
                else: #if self.use_database
                    self.psqlCursor.execute("SELECT * FROM calls WHERE incident_number = %s", (incident_number,))
                    if self.psqlCursor.fetchone() is None: # if incident number not found in DB
                        new_call_found = True
                        session['new_data'].append({
                            'url_location': call['location'],
                            'incident_number': incident_number,                    
                            'calldatetime': call['calldatetime'],
                            'problem_type': call['problem_type'],
                            'address': call['address'],
                            'division': call['division'],
                            'latitude': loc.latitude,  
                            'longitude': loc.longitude
                        })
                        self.psqlCursor.execute("INSERT INTO calls (url_location, incident_number, calldatetime, problem_type, address, division, latitude, longitude) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                                    (call['location'], incident_number, call['calldatetime'], call['problem_type'], call['address'], call['division'], loc.latitude, loc.longitude))
                        self.psqlConn.commit()
                        callstring = (f"{call['calldatetime']}: Call for Service {incident_number} at {call['address']} added for user {user_id}.")
                        print(callstring.replace("\n", ""))
        if new_call_found == False:
            print(f"No new calls found. Waiting to fetch more...")
        if self.args.debugmode:
            print(f"\Debugmode: End of get_data: added_calls = {session['new_data']}\n")
        print("\n")
        return jsonify(session['new_data'])
    
    def get_viewstate(self, user_id):
        session_info = self.get_session(user_id)
        session = session_info['session']
        if self.args.debugmode:
            print("grabbing brand new unused viewstate from GET")
        response = session.get(self.url)
        soup = BeautifulSoup(response.text, 'html.parser')
        viewstate = soup.find('input', {'name': '__VIEWSTATE'})
        viewstate = viewstate['value'] if viewstate else None
        eventvalidation = soup.find('input', {'name': '__EVENTVALIDATION'})
        eventvalidation = eventvalidation['value'] if eventvalidation else None
        viewstategenerator = soup.find('input', {'name': '__VIEWSTATEGENERATOR'})
        viewstategenerator = viewstategenerator['value'] if viewstategenerator else None
        session_info['viewstate'] = viewstate
        session_info['eventvalidation'] = eventvalidation
        session_info['viewstategenerator'] = viewstategenerator
            
        
    def post_Viewstate(self, user_id):
        session_info = self.get_session(user_id)
        session = session_info['session']
        if session_info['viewstate'] is None:
            self.get_viewstate(user_id)
        eventtarget = "Timer1" 
        eventargument = "" 
        viewstate = session_info['viewstate']
        eventvalidation = session_info['eventvalidation']
        viewstategenerator = session_info['viewstategenerator']

        headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Content-Type': 'application/x-www-form-urlencoded',
            'DNT': '1',
            'Origin': 'https://webapp3.sanantonio.gov',
            'Pragma': 'no-cache',
            'Referer': 'https://webapp3.sanantonio.gov/policecalls/Calls.aspx',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-GPC': '1',
            'Upgrade-Insecure-Requests': '1',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36'
        }
        data = {
        '__EVENTTARGET': eventtarget,
        '__EVENTARGUMENT': eventargument,
        '__VIEWSTATE': viewstate,
        '__VIEWSTATEGENERATOR': viewstategenerator,
        '__EVENTVALIDATION': eventvalidation,
        }
        response = session.post(self.url, headers=headers, data=data)
        if self.args.debugmode:
            print(f"Request details:")
            print(f"URL: {self.url}")
            print(f"Headers: {headers}")
            print(f"Data: {data}")
            print(f"\nResponse details:")
            print(f"Status code: {response.status_code}")
            print(f"Headers: {response.headers}")
            print(f"Content: {response.text}")
            print(f"\nSession cookies: {session.cookies}")
        soup = BeautifulSoup(response.text, 'html.parser')
        viewstate = soup.find('input', {'name': '__VIEWSTATE'})
        viewstate = viewstate['value'] if viewstate else None
        eventvalidation = soup.find('input', {'name': '__EVENTVALIDATION'})
        eventvalidation = eventvalidation['value'] if eventvalidation else None
        viewstategenerator = soup.find('input', {'name': '__VIEWSTATEGENERATOR'})
        viewstategenerator = viewstategenerator['value'] if viewstategenerator else None
        session_info['viewstate'] = viewstate
        session_info['eventvalidation'] = eventvalidation
        session_info['viewstategenerator'] = viewstategenerator
        if self.args.debugmode:
            print("New viewstate from POST set\n\n\n{session_info['viewstate']}")
        return response

    def get_locations(self, user_id):
        session_info = self.get_session(user_id)
        calls = []
        response = self.post_Viewstate(user_id)
        if self.args.debugmode:
            print("get_locations Post_Viewstate")
        soup = BeautifulSoup(response.text, 'html.parser')
        table = soup.find('table', {'id': 'gvCalls'})
        rows = table.find_all('tr')[1:] 
        for row in rows:
            columns = row.find_all('td')
            if len(columns) > 5:
                location_link = columns[4].find('a')['href']
                if 'q=' in location_link:
                    split_location = location_link.split('q=')
                    if len(split_location) > 1:
                        location = split_location[1]
                        incident_number = columns[1].text
                        calldatetime = columns[2].text
                        problem_type = columns[3].text
                        address = columns[4].text
                        division = columns[5].text
                        call = {
                            'location': location,
                            'incident_number': incident_number,
                            'calldatetime': calldatetime,
                            'problem_type': problem_type,
                            'address': address,
                            'division': division
                        }
                        calls.append(call)
        return calls
    
    def do_geocode(self, location, attempt=1, max_attempts=6):
        geolocator = Nominatim(user_agent="Live SAPD Public Calls-for-Service Map", timeout=15)
        try:
            if self.args.debugmode:
                print(f"do_geocode {location}")
            return geolocator.geocode(location)
        except GeocoderTimedOut:
            if attempt <= max_attempts:
                print(f"Geocode timeout, retrying. Attempt {attempt}")
                return self.do_geocode(location, attempt=attempt+1)
            raise

    def generate_user_id(self):
        user_agent = request.headers.get('User-Agent')
        ip_address = request.remote_addr
        accept_language = request.headers.get('Accept-Language')
        encoding = request.headers.get('Accept-Encoding')
        raw_data = f"{user_agent}{ip_address}{accept_language}{encoding}"
        hashed_data = hashlib.sha256(raw_data.encode()).hexdigest()
        return hashed_data

    def print_user_id(self, size):
        user_id = self.generate_user_id()
        if size == "full":
            return user_id
        else:
            return ("{}…{}".format(user_id[:4], user_id[-4:]))
# End of DataHandler class        


app = Flask(__name__)
app.secret_key = os.urandom(32)
data_handler = DataHandler(use_database)

logger = logging.getLogger('waitress')
logger.setLevel(logging.INFO)

@app.route('/')
def index():
    incident_number = request.args.get('incident_number')
    if incident_number:
        return render_template('index.html', incident_number=incident_number)
    else:
        return render_template('index.html')

@app.route('/allcalls/<int:page_num>')
def allcalls(page_num):
    if not data_handler.use_database:
        return '<b>The PostgreSQL database is not enabled.</b><br>Consider downloading PostgreSQL to setup a database that keeps your mapped call data between application uses. <a href="https://www.postgresql.org/">https://www.postgresql.org/<br><a href="/"><b>Go back to Map</b></a>'
    else:
        PER_PAGE = 50
        data_handler.psqlCursor.execute("SELECT * FROM calls ORDER BY TO_TIMESTAMP(calldatetime, 'MM/DD/YYYY HH12:MI:SS AM') DESC;")
        all_data = data_handler.psqlCursor.fetchall()
        total_pages = -(-len(all_data) // PER_PAGE)
        start = (page_num - 1) * PER_PAGE
        end = start + PER_PAGE
        page_data = []
        for row in all_data[start:end]:
            (url_location, incident_number, calldatetime, problem_type, address, division, latitude, longitude) = row
            call = {
                'incident_number': incident_number,
                'calldatetime': calldatetime,
                'problem_type': problem_type,
                'address': address,
                'division': division,
                'latitude': latitude,
                'longitude': longitude
            }
            page_data.append(call)
        return render_template('allcalls.html', data=page_data, total_pages=total_pages)

@app.route('/_fetch_old_data/')
def get_data_route():
    return data_handler.fetch_data_from_database()

@app.route('/_update_data/')
def update_data_route():
    return data_handler.get_data()

if __name__ == "__main__":
    if data_handler.args.host:
        user_host = "0.0.0.0"
    try:
        serve(app, host=user_host, port=user_port)
        print(f"Server successfully launched.")
    except:
        print(f"Error launching the web-server.")
