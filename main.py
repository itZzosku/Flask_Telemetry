from flask import Flask, Response, abort
from influxdb_client import InfluxDBClient
from dotenv import load_dotenv
import os
import re
from collections import OrderedDict
import json
import socket
import markdown
from waitress import serve
import diary

# Load environment variables from .env file
load_dotenv()


def check_env_variables():
    required_vars = ['INFLUXDB_URL', 'INFLUXDB_TOKEN', 'INFLUXDB_ORG', 'INFLUXDB_BUCKET']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing_vars)}")


check_env_variables()

app = Flask(__name__)

# Shooting diary (password-protected) at /diary
diary.init_app(app)


# Landing page route with links
@app.route('/')
def index():
    note_links = "".join(
        '\n    <p>Click <a href="/notes/{slug}">here</a> to open: {title}.</p>'.format(
            slug=slug, title=note["title"])
        for slug, note in list_notes().items()
    )
    return '''
    <h1>Flask is running!</h1>
    <p>Click <a href="/house">here</a> to get telemetry of the house.</p>
    <p>Click <a href="/ammo">here</a> to open the ammo cost calculator.</p>
    <p>Click <a href="/diary/">here</a> to open the shooting diary (login required).</p>''' + note_links + '''
    <p>Click <a href="https://github.com/itZzosku/Scope_Adjustment_Calculator_App/releases/latest">here</a> to get latest version of the Scope Adjustment Calculator App.</p>
    '''


# InfluxDB settings from environment variables
influxdb_url = os.getenv('INFLUXDB_URL')
token = os.getenv('INFLUXDB_TOKEN')
org = os.getenv('INFLUXDB_ORG')
bucket = os.getenv('INFLUXDB_BUCKET')

# Initialize InfluxDB client
client = InfluxDBClient(url=influxdb_url, token=token, org=org)
query_api = client.query_api()


def construct_flux_query():
    # Returns the Flux query string
    return '''
        from(bucket: "House Telemetry")
        |> range(start: -1h)
        |> filter(fn: (r) => r["_measurement"] == "ESP32")
        |> filter(fn: (r) => r["Name"] == "Telemetry")
        |> filter(fn: (r) =>
            r["_field"] == "Pressure" or
            r["_field"] == "Humidity" or
            r["_field"] == "Sensor" or
            r["_field"] == "Temperature")
        |> last()
    '''


def process_query_results(result):
    # Initialize an OrderedDict to hold the sensor data in the desired order
    sensor_data = OrderedDict([
        ("Temperature", None),
        ("Humidity", None),
        ("Pressure", None),
        ("Sensor", None)
    ])

    for table in result:
        for record in table.records:
            field = record.get_field()
            value = record.get_value()
            # Check if the field is one of the keys we're interested in, then update its value
            if field in sensor_data:
                sensor_data[field] = str(value)  # Convert value to string
    return sensor_data


@app.route('/house')
def get_data():
    try:
        flux_query = construct_flux_query()
        result = query_api.query(org=org, query=flux_query)
        sensor_data = process_query_results(result)
        json_data = json.dumps(sensor_data)
        return Response(json_data, mimetype='application/json')
    except Exception as e:
        print(f"Error querying InfluxDB: {e}")
        abort(500, description="Internal Server Error while querying InfluxDB")


# Per-round prices in EUR. Set these to your website's current prices.
AMMO_PRICES = OrderedDict([
    (".223 Rem",  0.60),
    ("9 mm",      0.30),
    ("12 gauge",  0.40),
    ("7.62x54R",  1.00),
    ("6.5 CM",    2.00),
])


# Ammo cost calculator: set price per round x rounds used -> total (client-side)
def build_ammo_page():
    rows = ""
    for name, price in AMMO_PRICES.items():
        key = name.replace(" ", "_").replace(".", "")
        rows += '''
      <div class="cart" data-key="{key}">
        <div class="name">{name}</div>
        <div class="row">
          <div>
            <label>Price / round (&euro;)</label>
            <input type="number" class="price" step="0.001" value="{price}">
          </div>
          <div>
            <label>Rounds used</label>
            <input type="number" class="count" step="1" min="0" value="0">
          </div>
        </div>
        <div class="sub"><span>Subtotal</span><span class="subval">&euro;0.00</span></div>
      </div>'''.format(key=key, name=name, price=price)

    return '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ammo Cost Calculator</title>
<style>
  :root {
    --bg: #14171c;
    --card: #1c2027;
    --line: #2c313b;
    --text: #e6e9ef;
    --muted: #8a93a3;
    --accent: #5aa9e6;
    --good: #6bcf8e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.5 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    padding: 24px 16px 64px;
  }
  .wrap { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub-title { color: var(--muted); margin: 0 0 24px; font-size: 13px; }
  .cart {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 16px;
    margin-bottom: 14px;
  }
  .name { font-size: 16px; font-weight: 600; margin-bottom: 12px; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 0 0 4px; }
  input {
    width: 100%;
    background: #0f1216;
    border: 1px solid var(--line);
    border-radius: 6px;
    color: var(--text);
    padding: 8px 10px;
    font-size: 15px;
  }
  input:focus { outline: none; border-color: var(--accent); }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  .sub {
    display: flex;
    justify-content: space-between;
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid var(--line);
    font-size: 13px;
    color: var(--muted);
  }
  .sub .subval { color: var(--text); font-variant-numeric: tabular-nums; }
  .total {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 20px;
    margin-top: 6px;
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }
  .total .tval { font-size: 30px; font-weight: 700; color: var(--good); font-variant-numeric: tabular-nums; }
  .foot { display: flex; justify-content: space-between; margin-top: 16px; align-items: center; }
  a.back { color: var(--accent); font-size: 13px; text-decoration: none; }
  button {
    background: transparent;
    border: 1px solid var(--line);
    color: var(--muted);
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 13px;
    cursor: pointer;
  }
  button:hover { border-color: var(--accent); color: var(--text); }
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/">&larr; back</a>
  <h1>Ammo Cost Calculator</h1>
  <p class="sub-title">Enter rounds used per caliber. Prices are per round &mdash; edit to match the website.</p>

  ''' + rows + '''

  <div class="total">
    <span>Total</span>
    <span class="tval" id="grandTotal">&euro;0.00</span>
  </div>

  <div class="foot">
    <span style="color:var(--muted);font-size:13px;" id="roundCount">0 rounds</span>
    <button id="reset">Reset counts</button>
  </div>
</div>

<script>
  var carts = Array.prototype.slice.call(document.querySelectorAll('.cart'));

  function num(el) { var v = parseFloat(el.value); return isFinite(v) ? v : 0; }

  function calc() {
    var total = 0, roundsTotal = 0, store = {};
    carts.forEach(function(c) {
      var price = num(c.querySelector('.price'));
      var count = num(c.querySelector('.count'));
      var sub = price * count;
      total += sub;
      roundsTotal += count;
      c.querySelector('.subval').textContent = '\\u20ac' + sub.toFixed(2);
      store[c.dataset.key] = { price: c.querySelector('.price').value, count: c.querySelector('.count').value };
    });
    document.getElementById('grandTotal').textContent = '\\u20ac' + total.toFixed(2);
    document.getElementById('roundCount').textContent = roundsTotal + (roundsTotal === 1 ? ' round' : ' rounds');
    try { localStorage.setItem('ammoCost', JSON.stringify(store)); } catch(e) {}
  }

  (function restore() {
    try {
      var s = JSON.parse(localStorage.getItem('ammoCost') || '{}');
      carts.forEach(function(c) {
        var d = s[c.dataset.key];
        if (d) {
          if (d.price !== undefined) c.querySelector('.price').value = d.price;
          if (d.count !== undefined) c.querySelector('.count').value = d.count;
        }
      });
    } catch(e) {}
  })();

  carts.forEach(function(c) {
    c.querySelector('.price').addEventListener('input', calc);
    c.querySelector('.count').addEventListener('input', calc);
  });

  document.getElementById('reset').addEventListener('click', function() {
    carts.forEach(function(c) { c.querySelector('.count').value = 0; });
    calc();
  });

  calc();
</script>
</body>
</html>'''


@app.route('/ammo')
def ammo_calculator():
    return Response(build_ammo_page(), mimetype='text/html')


# Markdown notes: drop a .md file into the notes/ folder and it shows up on the site.
NOTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notes')


def note_title(path, stem):
    """Use the file's first '# ' heading as the title, fall back to the filename."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('# '):
                    return line[2:].strip()
    except OSError:
        pass
    return stem.replace('_', ' ').replace('-', ' ').strip().capitalize()


def list_notes():
    """Scan notes/ and return an OrderedDict of slug -> {title, file}."""
    notes = OrderedDict()
    if not os.path.isdir(NOTES_DIR):
        return notes
    for filename in sorted(os.listdir(NOTES_DIR)):
        if not filename.lower().endswith('.md'):
            continue
        stem = os.path.splitext(filename)[0]
        slug = re.sub(r'[^a-z0-9]+', '-', stem.lower()).strip('-')
        if not slug:
            continue
        notes[slug] = {
            "title": note_title(os.path.join(NOTES_DIR, filename), stem),
            "file": filename,
        }
    return notes


NOTE_PAGE_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #14171c;
    --card: #1c2027;
    --line: #2c313b;
    --text: #e6e9ef;
    --muted: #8a93a3;
    --accent: #5aa9e6;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.6 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    padding: 24px 16px 64px;
  }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}
  a.back {{ color: var(--accent); font-size: 13px; text-decoration: none; }}
  .note {{
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 20px 24px;
    margin-top: 12px;
  }}
  .note h1 {{ font-size: 22px; margin: 0 0 16px; }}
  .note h2 {{ font-size: 17px; margin: 28px 0 10px; color: var(--accent); }}
  .note p {{ margin: 10px 0; }}
  .note hr {{ border: 0; border-top: 1px solid var(--line); margin: 24px 0; }}
  .note em {{ color: var(--muted); }}
  .note ul {{ padding-left: 20px; }}
  .note li {{ margin: 4px 0; }}
  .note code {{
    background: #0f1216;
    border: 1px solid var(--line);
    border-radius: 4px;
    padding: 1px 5px;
    font-size: 13px;
  }}
  .table-scroll {{ overflow-x: auto; }}
  .note table {{
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
    font-variant-numeric: tabular-nums;
  }}
  .note th, .note td {{
    border: 1px solid var(--line);
    padding: 8px 10px;
    text-align: left;
  }}
  .note th {{ background: #0f1216; color: var(--muted); font-size: 13px; font-weight: 600; }}
  .note tbody tr:nth-child(even) {{ background: rgba(255, 255, 255, 0.02); }}
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/">&larr; back</a>
  <div class="note">
{body}
  </div>
</div>
</body>
</html>'''


def render_note(filename):
    """Read a markdown file from notes/ and return it as HTML."""
    path = os.path.join(NOTES_DIR, filename)
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()

    # Task list checkboxes -> symbols (python-markdown has no task list support)
    text = re.sub(r'^(\s*[-*] )\[x\] ', r'\1☑ ', text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r'^(\s*[-*] )\[ \] ', r'\1☐ ', text, flags=re.MULTILINE)

    html = markdown.markdown(text, extensions=['tables'])
    # Let wide tables scroll instead of breaking the layout on a phone
    html = html.replace('<table>', '<div class="table-scroll"><table>')
    html = html.replace('</table>', '</table></div>')
    return html


@app.route('/notes/<slug>')
def note_page(slug):
    note = list_notes().get(slug)
    if note is None:
        abort(404)
    try:
        body = render_note(note["file"])
    except OSError:
        abort(404, description="Note file not found")
    page = NOTE_PAGE_TEMPLATE.format(title=note["title"], body=body)
    return Response(page, mimetype='text/html')


def get_local_ip():
    """Function to get the local IP address of the machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't even have to be reachable
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'  # Fallback to localhost if unable to determine local IP
    finally:
        s.close()
    return IP


if __name__ == '__main__':
    local_ip = get_local_ip()
    port = 5000
    print("Flask app starting")
    print(f"http://{local_ip}:{port}/")
    print(f"http://{local_ip}:{port}/house")
    print(f"http://{local_ip}:{port}/ammo")
    print(f"http://{local_ip}:{port}/diary/")
    for slug in list_notes():
        print(f"http://{local_ip}:{port}/notes/{slug}")
    serve(app, host=local_ip, port=port)
