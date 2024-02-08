from flask import Flask, Response, abort
from influxdb_client import InfluxDBClient
from dotenv import load_dotenv
import os
from collections import OrderedDict
import json

# Load environment variables from .env file
load_dotenv()


def check_env_variables():
    required_vars = ['INFLUXDB_URL', 'INFLUXDB_TOKEN', 'INFLUXDB_ORG', 'INFLUXDB_BUCKET']
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing_vars)}")


check_env_variables()

app = Flask(__name__)

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
        |> range(start: -60m)
        |> filter(fn: (r) => r["_measurement"] == "ESP32")
        |> filter(fn: (r) => r["Name"] == "Telemetry")
        |> filter(fn: (r) => r["_field"] == "Pressure" or r["_field"] == "Humidity" or r["_field"] == "Sensor" or r["_field"] == "Temperature")
        |> aggregateWindow(every: 10s, fn: last, createEmpty: false)
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


if __name__ == '__main__':
    app.run(debug=True)
